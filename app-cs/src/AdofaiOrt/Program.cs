using AdofaiStudio.BsrInference;
using System.Text;

// 钉死 stdout 为 UTF-8：dotnet 宿主在管道重定向（被 Python subprocess 调用）时，
// 默认会按系统代码页(本机 cp936/GBK)写诊断输出，含非 ASCII 字节，导致宿主侧 utf-8 解码失败。
Console.OutputEncoding = Encoding.UTF8;

// AdofaiOrt — 独立 C# ONNX 推理 CLI（替代 Python 的 onnxruntime 调用）。
//
// 设计：Python 侧（osn1_pipeline / osn1_stemjson）把 ONNX 推理外包给本 CLI，
// 自己只保留 librosa 的 decode/mel/谱通量 + JSON 组装（按主人 9-26 裁定：
// 音频前端留在 Python，C# 只接管 ONNX 推理）。本 CLI 是纯增量的薄文件 IO 外壳，
// 所有数值计算都在已逐位验证的 BsrInference 库里。
//
// 子命令：
//   onsetnet <mel.bin(C,128,T)> <model.onnx> <logits.out(T,)>
//       —— 替代 Python onnx_onsets / run_osn1 的 sess.run（C=通道数，现 7）
//   bsr-separate <mix.bin(1,2,N)> <stems.out(6,N)> [meta.json]
//       —— 替代 Python bsr_separate 的单块 sess.run + iSTFT（多块 OLA 仍在 Python 侧做）
//
// 默认走 CPU EP（确定性、便于逐位比对）；生产传 --cuda 走 CUDA EP 保 4.1GB 安全档。

Console.WriteLine($"[AdofaiOrt] START args={args.Length}");
if (args.Length < 1)
{
    Console.Error.WriteLine("usage: AdofaiOrt <onsetnet|bsr-separate> ...");
    return 2;
}
bool useCuda = args.Contains("--cuda");
var rest = args.Where(a => a != "--cuda").ToArray();

try
{
    switch (rest[0])
    {
        case "onsetnet":
            return CmdOnsetNet(rest, useCuda);
        case "bsr-separate":
            return CmdBsrSeparate(rest, useCuda);
        case "bsr-stft":
            return CmdBsrStft(rest, useCuda);
        default:
            Console.Error.WriteLine($"unknown subcommand: {rest[0]}");
            return 2;
    }
}
catch (Exception e)
{
    Console.Error.WriteLine($"[AdofaiOrt] ERROR: {e.Message}");
    return 1;
}

int CmdOnsetNet(string[] a, bool useCuda)
{
    if (a.Length < 4)
    {
        Console.Error.WriteLine("usage: AdofaiOrt onsetnet <mel.bin> <model.onnx> <logits.out> [--cuda]");
        return 2;
    }
    float[] mel = ReadFloats(a[1]);
    using var sess = new OnsetNetSession(a[2], useCuda);
    float[] logits = sess.Run(mel);
    WriteFloats(a[3], logits);
    Console.WriteLine($"[AdofaiOrt] onsetnet 完成: mel={mel.Length} -> logits={logits.Length}");
    return 0;
}

int CmdBsrSeparate(string[] a, bool useCuda)
{
    if (a.Length < 3)
    {
        Console.Error.WriteLine("usage: AdofaiOrt bsr-separate <mix.bin> <stems.out> [meta.json] [--cuda]");
        return 2;
    }
    string meta = a.Length >= 4 ? a[3]
        : MetaPathDefault();
    float[] mix = ReadFloats(a[1]);
    using var split = new BsrSplitSession(meta, useCuda);
    float[] stft = split.Run(mix);
    float[][] stems = Istft.Separate(stft);          // float[6][N]
    int n = stems[0].Length;
    float[] outp = new float[6 * n];
    for (int s = 0; s < 6; s++) Array.Copy(stems[s], 0, outp, s * n, n);
    WriteFloats(a[2], outp);
    Console.WriteLine($"[AdofaiOrt] bsr-separate 完成: mix={mix.Length} -> 6 源 stems={outp.Length}");
    return 0;
}

// bsr-stft：与 bsr-separate 同理，但输出 stft_real (12,1025,F,2) 扁平，
// 让 Python bsr_separate 的 _bsr_istft_chunk + _ola_window 逻辑原样复用（不碰已确认正确的部分）。
int CmdBsrStft(string[] a, bool useCuda)
{
    if (a.Length < 3)
    {
        Console.Error.WriteLine("usage: AdofaiOrt bsr-stft <mix.bin> <stft.out> [meta.json] [--cuda]");
        return 2;
    }
    string meta = a.Length >= 4 ? a[3]
        : MetaPathDefault();
    float[] mix = ReadFloats(a[1]);
    using var split = new BsrSplitSession(meta, useCuda);
    float[] stft = split.Run(mix);
    WriteFloats(a[2], stft);
    Console.WriteLine($"[AdofaiOrt] bsr-stft 完成: mix={mix.Length} -> stft_real={stft.Length}");
    return 0;
}

// 默认 meta 路径：相对 bundle 根定位，彻底告别写死 <REPO>。
// AdofaiOrt.exe 位于 <bundle>/app-cs/out/Release/net10.0/，向上 4 层到 bundle 根，
// 再进 audio-sep/onnx_poc/_split_meta_g2.json。正常流程下 Python 会传绝对 meta，此值仅兜底。
static string MetaPathDefault()
{
    var root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", ".."));
    return Path.Combine(root, "audio-sep", "onnx_poc", "_split_meta_g2.json");
}

float[] ReadFloats(string path)
{
    byte[] b = File.ReadAllBytes(path);
    var arr = new float[b.Length / 4];
    Buffer.BlockCopy(b, 0, arr, 0, b.Length);
    return arr;
}

void WriteFloats(string path, float[] arr)
{
    byte[] b = new byte[arr.Length * 4];
    Buffer.BlockCopy(arr, 0, b, 0, b.Length);
    File.WriteAllBytes(path, b);
}
