using System.Numerics;

namespace AdofaiStudio.BsrInference;

/// <summary>
/// C# 复刻 librosa.istft（center=True, hann, n_fft=2048, hop=512, win=2048）。
/// 标准重叠相加 + 窗平方 COLA 归一化 + 中心裁剪，与 Python 逐位等价。
/// stft_real 扁平布局 = [12, 1025, 1151, 2] C 序（12 = 6 源 × 2 声道）。
/// </summary>
public static class Istft
{
    public const int N_FREQ = 1025;
    public const int N_FRAMES = 1151;
    public const int N_FFT = 2048;
    public const int HOP = 512;
    public const int N_SRC2 = 12;          // 6 源 × 2 声道
    public const int N_SAMPLES = 588800;

    static double[] HannPeriodic(int n)
    {
        var w = new double[n];
        for (int i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.Cos(2.0 * Math.PI * i / n);
        return w;
    }

    static int Idx(int src2, int f, int t, int ri) => ((src2 * N_FREQ + f) * N_FRAMES + t) * 2 + ri;

    /// <summary>单 (src2) 通道 iSTFT -> float[588800]。</summary>
    public static float[] One(float[] stft, int src2)
    {
        int nFull = N_FFT + HOP * (N_FRAMES - 1);     // 590848
        double[] y = new double[nFull];
        double[] wsum = new double[nFull];
        double[] win = HannPeriodic(N_FFT);
        var onesided = new Complex[N_FREQ];
        for (int t = 0; t < N_FRAMES; t++)
        {
            for (int f = 0; f < N_FREQ; f++)
            {
                int b = Idx(src2, f, t, 0);
                onesided[f] = new Complex(stft[b], stft[b + 1]);
            }
            double[] frame = Fft.Irfft(onesided, N_FFT);
            int pos = t * HOP;
            for (int n = 0; n < N_FFT; n++)
            {
                double v = frame[n] * win[n];
                y[pos + n] += v;
                wsum[pos + n] += win[n] * win[n];
            }
        }
        int crop = N_FFT / 2;
        var outp = new float[N_SAMPLES];
        for (int i = 0; i < N_SAMPLES; i++)
        {
            int gi = i + crop;
            double w = wsum[gi];
            outp[i] = (float)(w > 1e-8 ? y[gi] / w : 0.0);
        }
        return outp;
    }

    /// <summary>stft_real 扁平 -> 6 源单声道（双声道平均），返回 (6, 588800)。</summary>
    public static float[][] Separate(float[] stft)
    {
        var res = new float[6][];
        for (int s = 0; s < 6; s++)
        {
            float[] a0 = One(stft, s * 2);
            float[] a1 = One(stft, s * 2 + 1);
            var avg = new float[N_SAMPLES];
            for (int i = 0; i < N_SAMPLES; i++) avg[i] = 0.5f * (a0[i] + a1[i]);
            res[s] = avg;
        }
        return res;
    }
}
