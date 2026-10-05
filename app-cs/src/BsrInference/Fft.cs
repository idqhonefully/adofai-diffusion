using System.Numerics;

namespace AdofaiStudio.BsrInference;

/// <summary>
/// 自带 radix-2 迭代 FFT（不加 Math.NET，保持依赖最小）。
/// 仅用于 iSTFT 的 irfft，n_fft=2048 是 2 的幂，走快速路径。
/// </summary>
public static class Fft
{
    /// <summary>原地正向 FFT（无 1/N 归一化）。a.Length 须为 2 的幂。</summary>
    public static void Forward(Complex[] a)
    {
        int n = a.Length;
        if (n <= 1) return;
        // 位反转置换
        for (int i = 1, j = 0; i < n; i++)
        {
            int bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) (a[i], a[j]) = (a[j], a[i]);
        }
        for (int len = 2; len <= n; len <<= 1)
        {
            double ang = 2.0 * Math.PI / len;          // 正向旋转因子 exp(-2πi·k/N)
            Complex wlen = new Complex(Math.Cos(ang), -Math.Sin(ang));
            for (int i = 0; i < n; i += len)
            {
                Complex w = Complex.One;
                int half = len >> 1;
                for (int k = 0; k < half; k++)
                {
                    Complex u = a[i + k];
                    Complex v = a[i + k + half] * w;
                    a[i + k] = u + v;
                    a[i + k + half] = u - v;
                    w *= wlen;
                }
            }
        }
    }

    /// <summary>原地逆 FFT（含 1/N 归一化），输出为实数（调用方取 .Real）。
    /// IFFT(x) = conj(FFT(conj(x))) / N。</summary>
    public static void Inverse(Complex[] a)
    {
        int n = a.Length;
        for (int i = 0; i < n; i++) a[i] = Complex.Conjugate(a[i]);
        Forward(a);
        for (int i = 0; i < n; i++) a[i] = Complex.Conjugate(a[i]) / n;
    }

    /// <summary>实数逆 FFT：1025 个单边复系数 -> 2048 个实样本（与 scipy.fft.irfft 等价）。
    /// 设 DC(bin0) 与 Nyquist(bin1024) 虚部为 0（irfft 规范）。</summary>
    public static double[] Irfft(Complex[] onesided, int n)
    {
        int half = onesided.Length;            // 1025
        int nh = n / 2;                        // 1024
        var full = new Complex[n];
        for (int k = 0; k < half; k++)
        {
            double im = (k == 0 || k == nh) ? 0.0 : onesided[k].Imaginary;
            full[k] = new Complex(onesided[k].Real, im);
        }
        for (int k = 1; k < nh; k++)           // 共轭对称补全 1025..2047
        {
            int m = n - k;
            full[m] = Complex.Conjugate(full[k]);
        }
        Inverse(full);
        var outp = new double[n];
        for (int i = 0; i < n; i++) outp[i] = full[i].Real;
        return outp;
    }
}
