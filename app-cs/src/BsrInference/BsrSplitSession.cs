using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;

namespace AdofaiStudio.BsrInference;

/// <summary>
/// 低内存旋钮（env 守卫，默认全关 ⇒ 行为与历史一致）：
///   ADOFAI_ORT_NO_ARENA     =1 ⇒ so.EnableCpuMemArena=false（关 arena，内存即用即还）
///   ADOFAI_ORT_NO_MEMPATTERN=1 ⇒ so.EnableMemoryPattern=false（关内存模式规划）
///   ADOFAI_ORT_BASIC_OPT    =1 ⇒ GraphOptimizationLevel=ORT_ENABLE_BASIC（少融合、少中间态）
///   ADOFAI_ORT_MODEL_DIRECT =1 ⇒ session.use_ort_model_bytes_directly=1（模型文件直映射，省加载副本）
///   ADOFAI_ORT_PROFILE      =1 ⇒ 每跑完一段打印当前 WS（定位峰值段）
///   ADOFAI_ORT_STOP_AFTER   =k ⇒ 只跑前 k 段（诊断用，不产出合法 stft）
/// 目标：让 8GB 内存机也能走 CPU 兜底（基线峰值 ~4.2GB，需压到 ~2.5GB 内）。
/// </summary>
static class OrtTune
{
    public static bool Flag(string name)
        => bool.TryParse(Environment.GetEnvironmentVariable(name), out var v) && v;
    public static int Int(string name)
        => int.TryParse(Environment.GetEnvironmentVariable(name), out var v) ? v : 0;
}

[StructLayout(LayoutKind.Sequential)]
internal struct PROCESS_MEMORY_COUNTERS_EX
{
    public uint cb;
    public uint PageFaultCount;
    public ulong PeakWorkingSetSize;
    public ulong WorkingSetSize;
    public ulong QuotaPeakPagedPoolUsage;
    public ulong QuotaPagedPoolUsage;
    public ulong QuotaPeakNonPagedPoolUsage;
    public ulong QuotaNonPagedPoolUsage;
    public ulong PagefileUsage;
    public ulong PeakPagefileUsage;
    public ulong PrivateUsage;
}

internal static class MemProbe
{
    [DllImport("psapi.dll", SetLastError = true)]
    private static extern bool GetProcessMemoryInfo(IntPtr hProcess,
        out PROCESS_MEMORY_COUNTERS_EX counters, uint size);

    public static ulong CurrentWsBytes()
    {
        try
        {
            var p = System.Diagnostics.Process.GetCurrentProcess();
            if (GetProcessMemoryInfo(p.Handle, out var c, (uint)Marshal.SizeOf<PROCESS_MEMORY_COUNTERS_EX>()))
                return c.WorkingSetSize;
        }
        catch { }
        return 0;
    }

    public static void LogSeg(int j, int total)
    {
        if (!OrtTune.Flag("ADOFAI_ORT_PROFILE")) return;
        var ws = CurrentWsBytes() / (1024.0 * 1024.0 * 1024.0);
        Console.Error.WriteLine($"[BsrSplitSession][profile] seg={j + 1}/{total} WS={ws:F2}GB");
    }
}

/// <summary>
/// BSR 的【14 段图分割链】会话（C# 版，逐位等价替代 Python 的 BsrSplitSession）。
///
/// 行为严格复刻 Python 端 bsr_split_runner.BsrSplitSession（cache=False 默认）：
///   - 读 _split_meta_*.json，按 seg 顺序逐段【建-跑-删】会话（任一时刻仅 1 个在卡上），
///     把单体 ~7.8GB 显存峰值砍到安全档（g2 档 CUDA 实测 4116 MiB）。
///   - 每段从 state 取 s["in"] 张量喂入，输出按 ONNX【真实输出名】写回 state；
///     跑完按 _needed[j]（反向传播的中间张量依赖集）裁剪 state。
///   - 末尾 stft_real 单独 capture 返回。
///
/// 与 Python 端逐位等价已由 Python 侧 _run_gpu_peak.py 证明（同设备链 vs 整图 max|Δ|=0）；
/// C# 端口正确性用「切分链 vs 切分链」比对隔离（见 BsrInference.Poc）。
///
/// CPU EP 下与 Python 1.30.0 内核一致 → 预期 bit-exact（maxAbs=0）。
/// 生产换 Microsoft.ML.OnnxRuntime.Gpu + useCuda=true 走 CUDA EP（保持 4.1GB 安全档）。
/// </summary>
public sealed class BsrSplitSession : IDisposable
{
    private sealed class Tensor
    {
        public float[] Data = Array.Empty<float>();
        public int[] Shape = Array.Empty<int>();
    }

    private readonly string _metaDir;   // meta 文件所在目录
    private readonly string _parentDir;  // meta 目录的父目录
    private readonly bool _useCuda;
    private readonly List<string> _segFns = new();
    private readonly List<List<string>> _segIn = new();
    private readonly List<HashSet<string>> _needed = new();
    private const string FinalOut = "stft_real";

    public BsrSplitSession(string metaPath, bool useCuda = false)
    {
        var full = Path.GetFullPath(metaPath);
        _metaDir = Path.GetDirectoryName(full)!;
        _parentDir = Path.GetDirectoryName(_metaDir)!;
        _useCuda = useCuda;

        using var doc = JsonDocument.Parse(File.ReadAllText(metaPath));
        var segs = doc.RootElement.GetProperty("segs");
        foreach (var s in segs.EnumerateArray())
        {
            _segFns.Add(s.GetProperty("fn").GetString()!);
            var ins = new List<string>();
            foreach (var t in s.GetProperty("in").EnumerateArray()) ins.Add(t.GetString()!);
            _segIn.Add(ins);
        }

        // 反向传播计算 _needed[j]：段 j 跑完后，未来段(j+1..last)还会消费的张量集合。
        // 与 Python `acc` 累加逻辑一致：needed[j]=acc（不含段 j 自身输入），随后 acc |= segs_in[j]。
        var n = _segFns.Count;
        _needed = new List<HashSet<string>>(n);
        for (int i = 0; i < n; i++) _needed.Add(null!);   // 预分配索引位，供下方按索引赋值
        var acc = new HashSet<string>();
        for (int j = n - 1; j >= 0; j--)
        {
            _needed[j] = new HashSet<string>(acc);
            foreach (var t in _segIn[j]) acc.Add(t);
        }

        Console.WriteLine($"[BsrSplitSession] segs={_segFns.Count} useCuda={_useCuda} final={FinalOut}");
    }

    /// <summary>
    /// 解析子图相对路径。兼容两种布局：
    ///   (1) meta 与 14 段 onnx 同目录、fn 为裸名 → metaDir 命中；
    ///   (2) 当前工程布局：meta 在 onnx_poc/，但 fn 前缀为 onnx_poc/（相对 Python 的 HERE=audio-sep/）
    ///       → metaDir 拼出 onnx_poc/onnx_poc/… 不存在，回退 parentDir=audio-sep/ 命中。
    /// </summary>
    private string ResolveFn(string fn)
    {
        var a = Path.Combine(_metaDir, fn);
        if (File.Exists(a)) return a;
        return Path.Combine(_parentDir, fn);
    }

    private InferenceSession Mk(int j)
    {
        var path = ResolveFn(_segFns[j]);
        var so = new SessionOptions();
        so.GraphOptimizationLevel = OrtTune.Flag("ADOFAI_ORT_BASIC_OPT")
            ? GraphOptimizationLevel.ORT_ENABLE_BASIC
            : GraphOptimizationLevel.ORT_ENABLE_ALL;
        so.ExecutionMode = ExecutionMode.ORT_SEQUENTIAL;
        // 低内存档：默认不关 arena/pattern（历史行为不变）；低配机可用 env 关掉换更小峰值。
        if (OrtTune.Flag("ADOFAI_ORT_NO_ARENA")) so.EnableCpuMemArena = false;
        if (OrtTune.Flag("ADOFAI_ORT_NO_MEMPATTERN")) so.EnableMemoryPattern = false;
        if (OrtTune.Flag("ADOFAI_ORT_MODEL_DIRECT"))
            so.AddSessionConfigEntry("session.use_ort_model_bytes_directly", "1");
        if (_useCuda)
        {
            // ORT 1.30：V1 符号 OrtSessionOptionsAppendExecutionProvider_CUDA 已移除，
            // 必须走 OrtCUDAProviderOptions（→ ..._CUDA_V2 入口）。
            using var cudaOpt = new OrtCUDAProviderOptions();
            cudaOpt.UpdateOptions(new Dictionary<string, string> { ["device_id"] = "0" });
            so.AppendExecutionProvider_CUDA(cudaOpt);
        }
        return new InferenceSession(path, so);
    }

    /// <summary>
    /// feed: mix 扁平数组（C 序，长度 = 1*2*N，默认 N=588800）。
    /// 返回 stft_real 扁平数组（C 序，长度 = 12*1025*1151*2 = 28314600）。
    /// </summary>
    public float[] Run(float[] mixFlat, int[]? mixShape = null)
    {
        var state = new Dictionary<string, Tensor>();
        state["mix"] = new Tensor
        {
            Data = mixFlat,
            Shape = mixShape ?? new[] { 1, 2, mixFlat.Length / 2 },
        };

        float[]? capturedFinal = null;

        int stopAfter = OrtTune.Int("ADOFAI_ORT_STOP_AFTER");
        int last = stopAfter > 0 ? Math.Min(stopAfter, _segFns.Count) : _segFns.Count;

        for (int j = 0; j < last; j++)
        {
            using var so = Mk(j);   // cache=False：每段建完即用，using 结束即释放（不常驻全链）

            var inputs = new List<NamedOnnxValue>();
            foreach (var t in _segIn[j])
            {
                if (state.TryGetValue(t, out var ten))
                {
                    var dt = new DenseTensor<float>(ten.Data, ten.Shape);
                    inputs.Add(NamedOnnxValue.CreateFromTensor(t, dt));
                }
            }

            using var results = so.Run(inputs);
            foreach (var r in results)
            {
                var name = r.Name;                       // ONNX 真实输出名（等价于 Python so.get_outputs()）
                var t = r.AsTensor<float>();
                var dims = t.Dimensions;
                var shape = new int[dims.Length];         // 跨 ORT 版本稳妥：避开 ImmutableArray Select 歧义
                for (int k = 0; k < dims.Length; k++) shape[k] = (int)dims[k];
                var data = t.ToArray();                  // 扁平副本，脱离 results 生命周期
                state[name] = new Tensor { Data = data, Shape = shape };
                if (name == FinalOut) capturedFinal = data;
            }

            // 裁剪：只保留未来段会消费的张量（保证任一时刻仅本段会话在卡上）。
            var keep = _needed[j];
            var pruned = new Dictionary<string, Tensor>(keep.Count + 1);
            foreach (var kv in state)
                if (keep.Contains(kv.Key)) pruned[kv.Key] = kv.Value;
            state = pruned;

            MemProbe.LogSeg(j, last);   // ADOFAI_ORT_PROFILE=1 时逐段打印 WS
        }

        if (capturedFinal != null) return capturedFinal;
        if (stopAfter > 0) return Array.Empty<float>();   // 诊断模式：不产出合法 stft
        if (state.TryGetValue(FinalOut, out var f)) return f.Data;
        throw new InvalidOperationException("stft_real 未产出");
    }

    public void Dispose() { /* 会话均为 using 局部，无需额外释放 */ }
}
