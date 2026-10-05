using System.Runtime.InteropServices;

namespace AdofaiStudio.Native;

/// <summary>
/// 窗口的「系统集成」面 —— 让自绘标题栏的窗口在 Windows 眼里仍然是个正经窗口。
///
/// 为什么需要它：这个壳用 WindowStyle=None，标题栏、最小化/最大化/关闭三个按钮
/// 都是页面（HTML）自己画的。Windows 完全不知道那些按钮存在，于是：
///   · 悬停最大化按钮不会弹 Snap Layouts（Win11 的标志性交互）；
///   · 右键标题栏不会弹系统菜单（还原/移动/大小/最小化/最大化/关闭）；
///   · 用 Win+方向键、拖到屏幕边缘改变窗口状态时，页面上的按钮字形不会更新。
/// 这一层负责把前两件事补成系统原生行为，第三件事在 MainWindow 里做状态广播。
///
/// 所有常量按 WinUser.h 原文，不许改。
/// </summary>
public static class WindowShell
{
    // ---- GetWindowLongPtr 的索引 ----
    public const int GwlStyle = -16;
    public const int GwlExStyle = -20;

    // ---- 窗口样式位（WinUser.h）----
    public const long WsCaption = 0x00C00000L;
    public const long WsSysMenu = 0x00080000L;
    public const long WsThickFrame = 0x00040000L;
    public const long WsMinimizeBox = 0x00020000L;
    public const long WsMaximizeBox = 0x00010000L;
    public const long WsPopup = 0x80000000L;

    // 🔴 WS_EX_TOOLWINDOW 会让窗口从任务栏消失、Alt+Tab 也看不到；
    //    WS_EX_APPWINDOW 则强制它出现在任务栏。任务栏集成必须查这两位。
    public const long WsExToolWindow = 0x00000080L;
    public const long WsExAppWindow = 0x00040000L;
    public const long WsExNoActivate = 0x08000000L;
    public const long WsExLayered = 0x00080000L;

    // ---- 命中测试返回值（WM_NCHITTEST 的约定值，必须是这些魔数）----
    public const int HtClient = 1;
    public const int HtCaption = 2;
    public const int HtMinButton = 8;
    public const int HtMaxButton = 9;
    public const int HtLeft = 10;
    public const int HtRight = 11;
    public const int HtTop = 12;
    public const int HtClose = 20;

    // ---- 窗口消息 ----
    public const int WmSize = 0x0005;
    public const int WmNcHitTest = 0x0084;
    public const int WmNcMouseMove = 0x00A0;
    public const int WmNcMouseLeave = 0x02A2;
    public const int WmNcLButtonDown = 0x00A1;
    public const int WmNcRButtonUp = 0x00A5;
    public const int WmSysCommand = 0x0112;
    public const int WmNull = 0x0000;

    // ---- TrackPopupMenuEx 标志 ----
    private const uint TpmLeftAlign = 0x0000;
    private const uint TpmTopAlign = 0x0000;
    private const uint TpmRightButton = 0x0002;
    private const uint TpmReturnCmd = 0x0100;

    // ---- EnableMenuItem / SetMenuDefaultItem ----
    private const uint MfByCommand = 0x00000000;
    private const uint MfEnabled = 0x00000000;
    private const uint MfGrayed = 0x00000001;

    // ---- 系统菜单命令 id ----
    private const uint ScSize = 0xF000;
    private const uint ScMove = 0xF010;
    private const uint ScMinimize = 0xF020;
    private const uint ScMaximize = 0xF030;
    private const uint ScClose = 0xF060;
    private const uint ScRestore = 0xF120;

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtrW(IntPtr hwnd, int index);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtrW(IntPtr hwnd, int index, IntPtr value);

    [DllImport("user32.dll")]
    private static extern IntPtr GetSystemMenu(IntPtr hwnd, bool revert);

    [DllImport("user32.dll")]
    private static extern bool EnableMenuItem(IntPtr hmenu, uint item, uint enable);

    [DllImport("user32.dll")]
    private static extern bool SetMenuDefaultItem(IntPtr hmenu, uint item, uint byPos);

    [DllImport("user32.dll")]
    private static extern int TrackPopupMenuEx(IntPtr hmenu, uint flags, int x, int y, IntPtr hwnd, IntPtr param);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, IntPtr pid);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint attach, uint attachTo, bool fAttach);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern bool PostMessageW(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessageW(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out PointStruct p);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hwnd, out RectStruct r);

    [DllImport("user32.dll")]
    private static extern bool ClientToScreen(IntPtr hwnd, ref PointStruct p);

    [StructLayout(LayoutKind.Sequential)]
    public struct RectStruct
    {
        public int Left, Top, Right, Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PointStruct
    {
        public int X, Y;
    }

    public static long ReadStyle(IntPtr hwnd) => GetWindowLongPtrW(hwnd, GwlStyle).ToInt64();
    public static long ReadExStyle(IntPtr hwnd) => GetWindowLongPtrW(hwnd, GwlExStyle).ToInt64();

    /// <summary>把样式位翻成人看得懂的一行，写日志用。</summary>
    public static string DescribeStyle(long s)
    {
        string Mark(long bit) => (s & bit) != 0 ? "✓" : "✗";
        return $"CAPTION {Mark(WsCaption)} | SYSMENU {Mark(WsSysMenu)} | THICKFRAME {Mark(WsThickFrame)} | "
             + $"MINBOX {Mark(WsMinimizeBox)} | MAXBOX {Mark(WsMaximizeBox)} | POPUP {Mark(WsPopup)}";
    }

    public static string DescribeExStyle(long e)
    {
        string Mark(long bit) => (e & bit) != 0 ? "✓" : "✗";
        return $"APPWINDOW {Mark(WsExAppWindow)} | TOOLWINDOW {Mark(WsExToolWindow)} | "
             + $"NOACTIVATE {Mark(WsExNoActivate)} | LAYERED {Mark(WsExLayered)}";
    }

    /// <summary>
    /// 补齐"能被系统当成正经窗口对待"所需的样式位。
    ///
    /// 缺 WS_MAXIMIZEBOX / WS_THICKFRAME 时，Win+↑ 最大化、Win+←/→ 分屏、拖到屏幕边缘
    /// Snap 全部失灵（系统认为这个窗口不可缩放/不可最大化）；缺 WS_SYSMENU 时 Alt+Space
    /// 不出系统菜单。反过来 WS_EX_TOOLWINDOW 会让窗口从任务栏和 Alt+Tab 里消失。
    ///
    /// 🔴 绝不碰 WS_CAPTION —— 给 WindowStyle=None 的窗口加上它，Windows 会真的画出一条
    ///    灰色原生标题栏，我们的无边框外观当场报废。WS_POPUP 同理不碰。
    /// </summary>
    public static bool EnsureFrameStyle(IntPtr hwnd, out long before, out long after)
    {
        before = ReadStyle(hwnd);
        var want = before | WsSysMenu | WsMinimizeBox | WsMaximizeBox | WsThickFrame;
        after = want;
        if (want == before) return false;

        SetWindowLongPtrW(hwnd, GwlStyle, (IntPtr)want);
        after = ReadStyle(hwnd);
        return after != before;
    }

    /// <summary>窗口若被标成 TOOLWINDOW 就摘掉（否则任务栏/Alt+Tab 看不见它）。</summary>
    public static bool EnsureTaskbarVisible(IntPtr hwnd, out long before, out long after)
    {
        before = ReadExStyle(hwnd);
        var want = before & ~WsExToolWindow;
        if (want == before)
        {
            after = before;
            return false;
        }
        SetWindowLongPtrW(hwnd, GwlExStyle, (IntPtr)want);
        after = ReadExStyle(hwnd);
        return after != before;
    }

    /// <summary>
    /// 弹**系统原生**菜单（就是 Alt+Space 那个，也是真 Windows 应用右键标题栏出来的那个）。
    /// 用 TPM_RETURNCMD 自己收返回值再回喂 WM_SYSCOMMAND —— 这样代码只处理自己该处理的，
    /// 移动/大小/还原这些交给系统的默认处理，不用我们自己实现。
    ///
    /// <paramref name="foregroundOk"/> 回传"有没有真的把窗口设成前台"。
    /// 🔴 这个信号必须回传，不能吞掉：设前台失败时菜单**照样会弹出来**（屏幕上能看到
    ///    #32768 那个弹窗），但它不吃输入、点外面也关不掉，宿主就永远卡在 TrackPopupMenu
    ///    里 —— 表现出来就是"壳死了"。不把这件事报出来，就只能靠猜。
    /// </summary>
    public static bool ShowSystemMenu(IntPtr hwnd, bool maximized, bool canMaximize,
                                      bool canMinimize, out bool foregroundOk)
    {
        foregroundOk = false;
        var menu = GetSystemMenu(hwnd, false);
        if (menu == IntPtr.Zero) return false;

        // 让菜单项状态跟上窗口当前状态（系统不一定会自动刷新这些）
        EnableMenuItem(menu, ScMaximize, MfByCommand | (canMaximize && !maximized ? MfEnabled : MfGrayed));
        EnableMenuItem(menu, ScMinimize, MfByCommand | (canMinimize ? MfEnabled : MfGrayed));
        EnableMenuItem(menu, ScRestore, MfByCommand | (maximized ? MfEnabled : MfGrayed));
        // 加粗项 = 双击标题栏会执行的动作
        SetMenuDefaultItem(menu, maximized ? ScRestore : ScMaximize, 0);

        GetCursorPos(out var pt);

        // MSDN 明写：不先把窗口设成前台，菜单在外部点击时不会正常消失。
        // 🔴 光调 SetForegroundWindow 不够 —— 前台锁会让"当前不是前台的进程"这一句直接
        //    失败（实测踩到：菜单弹在屏幕上、前台却是别的窗口，整个壳像死了一样）。
        //    所以这里走 ForceForeground（借前台线程的输入队列）。
        foregroundOk = ForceForeground(hwnd);

        var cmd = TrackPopupMenuEx(menu, TpmReturnCmd | TpmLeftAlign | TpmTopAlign | TpmRightButton,
                                   pt.X, pt.Y, hwnd, IntPtr.Zero);
        // TrackPopupMenu 的经典怪癖：不补一条消息，菜单残影会留在屏幕上
        PostMessageW(hwnd, WmNull, IntPtr.Zero, IntPtr.Zero);

        if (cmd != 0)
            SendMessageW(hwnd, WmSysCommand, (IntPtr)cmd, IntPtr.Zero);
        return true;
    }

    /// <summary>
    /// 强行把窗口提到前台，返回是否**真的**成了。
    ///
    /// 为什么要这一坨：SetForegroundWindow 有个"前台锁"——不是前台的进程调它基本会失败，
    /// 而弹系统菜单**必须先成为前台**（否则菜单不吃输入）。标准解法是 AttachThreadInput
    /// 把自己的输入队列临时接到当前前台线程上，借它的权限设完再解绑。
    /// </summary>
    private static bool ForceForeground(IntPtr hwnd)
    {
        if (GetForegroundWindow() == hwnd) return true;
        if (SetForegroundWindow(hwnd) && GetForegroundWindow() == hwnd) return true;

        var fg = GetForegroundWindow();
        var tid = fg == IntPtr.Zero ? 0 : GetWindowThreadProcessId(fg, IntPtr.Zero);
        var me = GetCurrentThreadId();
        var attached = tid != 0 && tid != me && AttachThreadInput(me, tid, true);
        try
        {
            SetForegroundWindow(hwnd);
            Thread.Sleep(30);
            return GetForegroundWindow() == hwnd;
        }
        finally
        {
            if (attached) AttachThreadInput(me, tid, false);
        }
    }

    /// <summary>客户区左上角在屏幕上的坐标 —— WM_NCHITTEST 给的是屏幕坐标，换算要用它。</summary>
    public static PointStruct ClientOrigin(IntPtr hwnd)
    {
        var p = new PointStruct { X = 0, Y = 0 };
        ClientToScreen(hwnd, ref p);
        return p;
    }

    /// <summary>整个窗口（含边框）在屏幕上的矩形。用于判断命中点是不是落在这个窗口上。</summary>
    public static RectStruct WindowRect(IntPtr hwnd)
    {
        GetWindowRect(hwnd, out var r);
        return r;
    }

    /// <summary>把当前鼠标位置读出来（探针与"右键在哪弹菜单"都要用）。</summary>
    public static PointStruct CursorPos()
    {
        GetCursorPos(out var p);
        return p;
    }
}
