using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;

namespace AdofaiStudio.BsrInference;

/// <summary>
/// OnsetNet ONNX 推理会话（C# 版，替代 Python 的 get_osn1_session + onnxruntime 调用）。
///
/// 模型规格（models/osn1/onset_net.onnx）：
///   IN  mel [1, C, 128, T]   (C=通道数由 ONNX 输入维度推导，现 7ch 模型 C=7；T 动态，C 序展开 = 1*C*128*T)
///   OUT logits [1, T]        (raw logits；sigmoid 在调用方做，与 Python 一致)
///
/// 调用方负责把 mel 整理成 (C,128,T)：
///   - 7ch 综合模型（ONSET_CH7 = [bass, drums, other, vocals, guitar, piano, full]），
///     由 Python 侧 osn1_pipeline.build_7ch_mel 产出（原 6ch 的 accomp 冗余被丢弃，
///     guitar/piano 从合并 other 中拆成独立通道）。
///   - 全管线（detect + osn1_stemjson 的 melody/vocals/piano）统一走 onset_net.onnx：
///     · melody/人声/钢琴三路各自用 build_7ch_mel_focus 构造「以该音轨为主」的 7ch mel
///       （full 通道换成该音轨自身波形，其余 6 通道给真实 BSR 分离上下文），各自一次 7ch 推理；
///     · 主谱 detect 额外跑一路 lead-focused 7ch 视图，与综合 7ch 取并集（prob = max），
///       覆盖 only-other / 稀疏段落（综合模型熄火的主谱死区）；
///     · 能量门控阈值自适应（max(3e-4, 0.01×本曲中位 RMS)），不再写死 0.005。
///   2026-09-30：melody/vocal 专用权重视为用户失败品已删除，现全管线统一走 onset_net.onnx。
///   2026-10-02：通道数不再硬编码 6，改从 ONNX 输入维度推导（_inCh），7ch/未来更多通道自动适配。
///   2026-10-03：per-stem 路由从「一次综合推理 + 能量门控筛选」改为「各音轨各自喂 7ch 模型」
///     （7ch 回归三症状修复，详见 Python 侧 osn1_stemjson / osn1_pipeline）。
/// 本类只做 ORT 推理，不关心通道来源与 per-stem 编排。
/// CPU EP 下与 Python onnxruntime 1.30.0 内核一致 → 预期 bit-exact。
/// </summary>
public sealed class OnsetNetSession : IDisposable
{
    private readonly InferenceSession _sess;
    private readonly string _inputName;
    private readonly int _inCh;

    public OnsetNetSession(string modelPath, bool useCuda = false)
    {
        var so = new SessionOptions();
        so.GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_ALL;
        so.ExecutionMode = ExecutionMode.ORT_SEQUENTIAL;
        if (useCuda)
        {
            // 显存上限：封在 7000 MiB 内，避免 ORT CUDA EP 默认无上限(gpu_mem_limit=0)圈地，
            // 把整张 8GB 卡吃到见底、溢出到共享显存（笔记本 RTX 4070 8GB 设计目标）。
            // 模型+激活实际只需数百 MB，7000 MiB 上限既留足余量又不 spill。
            // 注：此 ORT 1.30 managed 构建的 OrtCUDAProviderOptions 无属性，需用 UpdateOptions 传原生键。
            var cudaOpts = new OrtCUDAProviderOptions();
            cudaOpts.UpdateOptions(new Dictionary<string, string>
            {
                ["device_id"] = "0",
                ["gpu_mem_limit"] = (7000UL * 1024UL * 1024UL).ToString(), // 7000 MiB
            });
            so.AppendExecutionProvider_CUDA(cudaOpts);
        }
        _sess = new InferenceSession(modelPath, so);
        _inputName = _sess.InputMetadata.Keys.First();
        var shp = _sess.InputMetadata[_inputName].Dimensions;
        // 通道数从模型输入维度推导（index 1），不再硬编码 6：7ch 模型即 7。
        // 时间维 T 是符号维（通常为 -1），不参与通道推导。
        if (shp.Length < 4)
            throw new InvalidOperationException($"ONNX 输入维度异常: [{string.Join(",", shp)}]");
        _inCh = (int)shp[1];
        Console.WriteLine($"[OnsetNet] input={_inputName} shape=[{string.Join(",", shp)}] inCh={_inCh} " +
                          $"provider={(useCuda ? "CUDA" : "CPU")} model={Path.GetFileName(modelPath)}");
    }

    /// <summary>
    /// melFlat: (C,128,T) C 序展开（C=模型通道数，现 7），长度 = C*128*T。T 由数据长度反推。
    /// 返回 logits (T,) 扁平数组（未做 sigmoid，与 Python sess.run 返回一致）。
    /// </summary>
    public float[] Run(float[] melFlat)
    {
        int T = melFlat.Length / (_inCh * 128);
        if (_inCh * 128 * T != melFlat.Length)
            throw new ArgumentException($"mel 长度 {melFlat.Length} 不是 {_inCh}*128*T");
        int[] shape = { 1, _inCh, 128, T };
        var tensor = new DenseTensor<float>(melFlat, shape);
        var inputs = new List<NamedOnnxValue> { NamedOnnxValue.CreateFromTensor(_inputName, tensor) };
        using var results = _sess.Run(inputs);
        return results[0].AsTensor<float>().ToArray();   // [1,T] -> 扁平 (T,)
    }

    public void Dispose() => _sess.Dispose();
}
