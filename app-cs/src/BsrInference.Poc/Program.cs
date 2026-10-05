using AdofaiStudio.BsrInference;

// POC：C# 跑 BSR 推理（单体 + 14 段切分链）+ iSTFT，与 Python 基准逐位比对。
// 基准由 audio-sep 的 Python 生成（onnx_poc/_poc_ref/*.bin）。
string refDir = @"<REPO>\audio-sep\onnx_poc\_poc_ref";
string monoModel = @"<REPO>\audio-sep\onnx_poc\bsr_sw_mha.onnx";
string splitMeta = @"<REPO>\audio-sep\onnx_poc\_split_meta_g2.json";

float[] mix = ReadFloats(Path.Combine(refDir, "mix.bin"));
float[] refStftMono = ReadFloats(Path.Combine(refDir, "stft_real.bin"));           // 单体 CPU 参考
float[] refStftSplit = ReadFloats(Path.Combine(refDir, "stft_real_split_cpu.bin")); // 切分链 CPU 参考
float[] refAudio = ReadFloats(Path.Combine(refDir, "audio.bin"));
string onsetModel = @"<REPO>\audio-sep\models\osn1\onset_net.onnx";
float[] mel7 = ReadFloats(Path.Combine(refDir, "osn1_mel7.bin"));            // (7,128,T) C 序（7ch 模型）
float[] refLogits = ReadFloats(Path.Combine(refDir, "osn1_onset_net_logits7.bin")); // (T,) raw，对应 onset_net.onnx（7ch）
Console.WriteLine($"[poc] mix={mix.Length} refStftMono={refStftMono.Length} " +
                  $"refStftSplit={refStftSplit.Length} refAudio={refAudio.Length} " +
                  $"mel7={mel7.Length} refLogits={refLogits.Length}");

// ---------- Phase 1a：单体会话（sanity，应与 refStftMono bit-exact）----------
double mxA1 = 0, rlA1 = 0;
using (var sess = new BsrSession(monoModel, useCuda: false))
{
    float[] stft = sess.Run(mix);
    (mxA1, rlA1) = Compare(stft, refStftMono);
    Console.WriteLine($"[poc] PHASE1a 单体 stft_real : maxAbsDiff={mxA1:E3}  relL2={rlA1:E3}");
}

// ---------- Phase 1b：14 段切分链（核心，验证 C# 端口 == Python 切分链）----------
double mxS = 0, rlS = 0;
using (var split = new BsrSplitSession(splitMeta, useCuda: false))
{
    float[] stftSplit = split.Run(mix);
    (mxS, rlS) = Compare(stftSplit, refStftSplit);
    Console.WriteLine($"[poc] PHASE1b 切分链 stft_real: maxAbsDiff={mxS:E3}  relL2={rlS:E3}");
}

// ---------- Phase 2：iSTFT（音频逐位比对，接切分链输出）----------
var splitOut = new BsrSplitSession(splitMeta, useCuda: false).Run(mix);
float[][] srcs = Istft.Separate(splitOut);
float[] flat = new float[6 * Istft.N_SAMPLES];
for (int s = 0; s < 6; s++) Array.Copy(srcs[s], 0, flat, s * Istft.N_SAMPLES, Istft.N_SAMPLES);
var (mxA, rlA) = Compare(flat, refAudio);
Console.WriteLine($"[poc] PHASE2  iSTFT audio    : maxAbsDiff={mxA:E3}  relL2={rlA:E3}");

// ---------- Phase 3：OnsetNet ONNX（验证 C# 端口 == Python onnxruntime）----------
double mxO = 0, rlO = 0;
using (var onset = new OnsetNetSession(onsetModel, useCuda: false))
{
    float[] logits = onset.Run(mel7);
    (mxO, rlO) = Compare(logits, refLogits);
    Console.WriteLine($"[poc] PHASE3 OnsetNet logits: maxAbsDiff={mxO:E3}  relL2={rlO:E3}");
}

bool ok = rlS < 1e-4 && rlA < 1e-4 && rlO < 1e-4;
Console.WriteLine(ok
    ? "[poc] ✅ 切分链端口 + iSTFT + OnsetNet 均逐位等价（relL2<1e-4）"
    : "[poc] ⚠ 存在差异，需排查");

static float[] ReadFloats(string path)
{
    byte[] bytes = File.ReadAllBytes(path);
    var arr = new float[bytes.Length / 4];
    Buffer.BlockCopy(bytes, 0, arr, 0, bytes.Length);
    return arr;
}

static (double maxAbs, double relL2) Compare(float[] a, float[] b)
{
    if (a.Length != b.Length) throw new Exception($"len mismatch {a.Length} vs {b.Length}");
    double sD = 0, sR = 0, mx = 0;
    for (int i = 0; i < a.Length; i++)
    {
        double d = a[i] - b[i];
        sD += d * d;
        sR += (double)b[i] * b[i];
        double ad = Math.Abs(d);
        if (ad > mx) mx = ad;
    }
    return (mx, Math.Sqrt(sD) / Math.Sqrt(sR));
}
