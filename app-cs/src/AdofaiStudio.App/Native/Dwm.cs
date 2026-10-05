using System.Runtime.InteropServices;

namespace AdofaiStudio.Native;

/// <summary>
/// DWM 窗口材质 —— 严格对齐老壳 gui_main.py 的 set_backdrop / set_round / set_dark /
/// extend_frame。数值不能改，前端按钮发出的 kind 就是这个约定：
///   1 = 无材质（透明/实色）  2 = 云母 Mica  3 = 亚克力 Acrylic
/// </summary>
public static class Dwm
{
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaWindowCornerPreference = 33;
    private const int DwmwaBorderColor = 34;
    private const int DwmwaCaptionColor = 35;
    private const int DwmwaTextColor = 36;
    private const int DwmwaSystemBackdropType = 38;

    // 🔴 这几个数别照着"枚举顺序"猜：微软的 DWM_SYSTEMBACKDROP_TYPE 是
    //    AUTO=0 / NONE=1 / MAINWINDOW(云母)=2 / TRANSIENTWINDOW(亚克力)=3 / TABBEDWINDOW(标签页)=**4**
    //    —— 「标签页」在最后，不是 3（3 已经被亚克力占了）。写错会静默变成别的材质。
    public const int BackdropAuto = 0;
    public const int BackdropNone = 1;
    public const int BackdropMica = 2;
    public const int BackdropAcrylic = 3;
    public const int BackdropTabbed = 4;

    /// <summary>DWMWA_COLOR_DEFAULT —— "让系统按默认来"（材质自己画标题栏时用它还原）。</summary>
    public const uint ColorDefault = 0xFFFFFFFF;

    private const int CornerDefault = 0;
    private const int CornerDoNotRound = 1;
    private const int CornerRound = 2;

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        public int Left, Right, Top, Bottom;
    }

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref Margins margins);

    /// <summary>设置窗口背板材质。kind 见类注释。</summary>
    public static int SetBackdrop(IntPtr hwnd, int kind)
    {
        var v = kind;
        return DwmSetWindowAttribute(hwnd, DwmwaSystemBackdropType, ref v, sizeof(int));
    }

    public static int SetRounded(IntPtr hwnd, bool round = true)
    {
        var v = round ? CornerRound : CornerDoNotRound;
        return DwmSetWindowAttribute(hwnd, DwmwaWindowCornerPreference, ref v, sizeof(int));
    }

    public static int SetDark(IntPtr hwnd, bool dark = true)
    {
        var v = dark ? 1 : 0;
        return DwmSetWindowAttribute(hwnd, DwmwaUseImmersiveDarkMode, ref v, sizeof(int));
    }

    /// <summary>
    /// 标题栏（非客户区那条）的**纯色**。传 <see cref="ColorDefault"/> 还原成"系统默认/跟随材质"。
    ///
    /// 🔴 为什么需要它：只有「实色」这一档是不施任何 DWM 材质的（kind=1）——
    ///    那时候标题栏没有材质可画，系统就按深/浅模式给一个**自己的默认色**
    ///    （深色模式下是纯黑 #000000）。于是出现主人截图里那个台阶：
    ///    **标题栏纯黑、客户区 #0E1116**，一眼就是两块颜色没对齐。
    ///    实色档必须显式把标题栏设成和客户区同一个底色；
    ///    云母/亚克力反过来 —— 材质会自己画标题栏，设了纯色反而把材质盖掉，所以要还原成 Default。
    /// </summary>
    public static int SetCaptionColor(IntPtr hwnd, uint colorRef)
    {
        var v = unchecked((int)colorRef);
        return DwmSetWindowAttribute(hwnd, DwmwaCaptionColor, ref v, sizeof(int));
    }

    /// <summary>标题栏文字色。传 <see cref="ColorDefault"/> 还原（一般不用设：明暗由 SetDark 决定）。</summary>
    public static int SetTextColor(IntPtr hwnd, uint colorRef)
    {
        var v = unchecked((int)colorRef);
        return DwmSetWindowAttribute(hwnd, DwmwaTextColor, ref v, sizeof(int));
    }

    /// <summary>窗口外那一圈 1px 描边。同样支持 <see cref="ColorDefault"/>。</summary>
    public static int SetBorderColor(IntPtr hwnd, uint colorRef)
    {
        var v = unchecked((int)colorRef);
        return DwmSetWindowAttribute(hwnd, DwmwaBorderColor, ref v, sizeof(int));
    }

    /// <summary>把 "#RRGGBB" 转成 Win32 的 COLORREF（0x00BBGGRR）。</summary>
    public static uint ColorRef(string hex)
    {
        var s = (hex ?? "").Trim().TrimStart('#');
        if (s.Length != 6 || !uint.TryParse(s, System.Globalization.NumberStyles.HexNumber, null, out var rgb))
            return ColorDefault;
        var r = (rgb >> 16) & 0xFF;
        var g = (rgb >> 8) & 0xFF;
        var b = rgb & 0xFF;
        return (b << 16) | (g << 8) | r;
    }

    /// <summary>颜色名/`#RRGGBB` → (R,G,B)，给 WebView 那层用。</summary>
    public static (byte R, byte G, byte B) Rgb(string hex)
    {
        var s = (hex ?? "").Trim().TrimStart('#');
        if (s.Length != 6 || !uint.TryParse(s, System.Globalization.NumberStyles.HexNumber, null, out var rgb))
            return (0x0E, 0x11, 0x16);
        return ((byte)((rgb >> 16) & 0xFF), (byte)((rgb >> 8) & 0xFF), (byte)(rgb & 0xFF));
    }

    /// <summary>
    /// 把材质延伸到整个客户区。缺这一步时，无边框窗口下 WebView2 的透明背景会糊成白底
    /// （老壳 gui_main.py 里同一行注释踩过这个坑）。
    /// </summary>
    public static int ExtendFrameIntoClientArea(IntPtr hwnd)
    {
        var m = new Margins { Left = -1, Right = -1, Top = -1, Bottom = -1 };
        return DwmExtendFrameIntoClientArea(hwnd, ref m);
    }
}
