using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;

namespace AdofaiStudio.BsrInference;

/// <summary>
/// BSR 分离 ONNX 会话（C# 版，替代 Python 的 BsrSplitSession / onnxruntime）。
/// 当前 POC 用单体模型 bsr_sw_mha.onnx（mix[1,2,588800] -> stft_real[12,1025,1151,2]），
/// 与分段链逐位等价。生产可换 14 段链或 Gpu NuGet 走 CUDA EP。
/// </summary>
public sealed class BsrSession : IDisposable
{
    private readonly InferenceSession _sess;
    private readonly string _inputName;
    private readonly int[] _inShape;

    public BsrSession(string modelPath, bool useCuda = false)
    {
        var so = new SessionOptions();
        so.GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_ALL;
        so.ExecutionMode = ExecutionMode.ORT_SEQUENTIAL;
        if (useCuda)
        {
            // 仅在生产（Microsoft.ML.OnnxRuntime.Gpu）时启用；POC 用 CPU 做逐位比对。
            so.AppendExecutionProvider_CUDA(0);
        }
        _sess = new InferenceSession(modelPath, so);
        _inputName = _sess.InputMetadata.Keys.First();
        _inShape = _sess.InputMetadata[_inputName].Dimensions.Select(d => (int)d).ToArray();
        Console.WriteLine($"[BsrSession] input={_inputName} shape=[{string.Join(",", _inShape)}] " +
                          $"provider={(useCuda ? "CUDA" : "CPU")}");
    }

    /// <summary>feed: float32 扁平数组（C 序，长度 = 1*2*N）。返回 stft_real 扁平数组（C 序）。</summary>
    public float[] Run(float[] mixFlat)
    {
        var tensor = new DenseTensor<float>(mixFlat, _inShape);
        var inputs = new List<NamedOnnxValue> { NamedOnnxValue.CreateFromTensor(_inputName, tensor) };
        using var results = _sess.Run(inputs);
        var outT = results[0].AsTensor<float>();
        return outT.ToArray();
    }

    public void Dispose() => _sess.Dispose();
}
