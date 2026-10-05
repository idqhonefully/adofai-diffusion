using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Shell;
using AdofaiStudio.Native;
using AdofaiStudio.Services;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace AdofaiStudio;

/// <summary>
/// C# 版原生壳 —— 对齐老壳 gui_main.py 的行为契约。
///
/// 结构对照：
///   gui_main.py 的 main()          → OnLoaded（起网关 → 起 sidecar → 导航）
///   backend.gateway_start()        → Services/Gateway.cs（同源网关 8766）
///   backend.start()                → Services/Sidecar.cs（chartgen 子进程）
///   handle_web_message() 那一大串 → Dispatch()（35 条消息分支）
///   send_history / do_*            → Services/HostApi.cs
/// </summary>
public partial class MainWindow : Window
{
    private CoreWebView2? _core;
    private MaterialSpec _material = Materials.Resolve(ReadPrefs("material"));
    /// <summary>深色 / 浅色。默认深色 —— 老壳只有深色，升级过来不该突然变白。</summary>
    private AppearanceSpec _appearance = Appearances.Resolve(ReadPrefs("appearance"));
    private readonly Gateway _gateway = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };

    /// <summary>「生成完 / 选了历史 MIDI」待载入工作台的文件，等页面 dsh_ready 后灌进去。</summary>
    private string? _pendingStudioLoad;

    // ---------------------------------------------------------------- 系统集成状态

    /// <summary>
    /// 窗口边框模式：
    ///   native（默认）—— Windows 自己画标题栏。图标、标题、三个按钮、悬停 Snap Layouts、
    ///                    右键系统菜单、双击最大化、Win+方向键，全是系统实现，我们一行都不用写。
    ///                    页面上那条自绘标题栏由注入 CSS 整条隐藏。
    ///   frameless     —— 上一版形态：无边框 + 页面自绘标题栏，靠 app-region 与命中测试模拟系统行为。
    /// 切换：ADOFAI_SHELL_CHROME=frameless。留着它是因为换边框模式属于"机器相关"的观感取向，
    /// 万一在别的机器上原生标题栏和材质搭不起来，一条环境变量就能退回，不用重新编译。
    /// </summary>
    private readonly string _chromeMode =
        (Environment.GetEnvironmentVariable("ADOFAI_SHELL_CHROME") ?? "native").Trim().ToLowerInvariant();

    /// <summary>当前是不是原生标题栏模式。</summary>
    private bool NativeChrome => _chromeMode != "frameless";

    /// <summary>
    /// WebView2 非客户区支持开关（= 让页面的 app-region 规则变成真·系统标题栏）。
    /// 关掉用环境变量：ADOFAI_SHELL_NC=0。
    /// 🔴 这个开关**只在无边框模式下有意义**：原生标题栏本身就是系统画的，
    ///    标题栏那片像素压根不归 WebView2 管，再开这个支持也没东西可支持。
    /// </summary>
    private readonly bool _ncRegion = EnvFlag("ADOFAI_SHELL_NC", true);

    private IntPtr _hwnd;
    private bool _wndProcHooked;

    /// <summary>上一次广播出去的"是否最大化"，用来只在真正变化时发消息（别刷屏）。</summary>
    private bool? _lastZoomed;

    /// <summary>
    /// 页面上「最大化」按钮矩形，**设备像素、相对客户区**。WM_NCHITTEST 要给屏幕坐标，
    /// 所以命中时还要加上客户区原点。由页面自报（见 QueryMaxButtonRectAsync）。
    /// </summary>
    private Rect _maxBtnRect = Rect.Empty;
    private int _hitTestInWindow;        // 落在本窗口内的命中测试次数（只统计窗口内的，别被窗外噪声吃掉）
    private int _hitTestMaxHits;         // 命中最大化按钮的次数（用来判定 Snap Layouts 这条路通不通）

    public MainWindow()
    {
        InitializeComponent();

        if (!NativeChrome) ApplyFramelessChrome();

        SourceInitialized += OnSourceInitialized;
        Loaded += OnLoaded;
        Closed += (_, _) => _gateway.Dispose();

        // 🔴 窗口状态改为「变化即广播」这一个唯一出口。
        //    以前只有"页面点了最大化按钮"那条路会发 winstate，于是用 Win+↑、拖到屏幕边缘
        //    分屏、双击标题栏这些**系统途径**改变状态时，页面上的按钮字形不会更新，
        //    下次再点就会反向（看着是"最大化"其实要还原）。这是真 bug，不是洁癖。
        StateChanged += (_, _) => PostWinState();

        KeyDown += (_, e) =>
        {
            // Alt+Space 走 WPF 默认处理不出来（无边框窗口），显式接住弹系统菜单
            if (e.Key == Key.Space && Keyboard.Modifiers == ModifierKeys.Alt)
            {
                ShowSystemMenu();
                e.Handled = true;
                return;
            }
            if (e.Key == Key.Escape) Close();
        };
    }

    // ---------------------------------------------------------------- 窗口边框

    /// <summary>
    /// 无边框模式（上一版形态）：<c>WindowStyle=None</c> + WindowChrome。
    ///
    /// 各参数为什么是这些值（都是踩出来的，别随手改）：
    ///   CaptionHeight=0      标题栏由页面自己画，所以不给系统留标题栏区。
    ///   ResizeBorderThickness=6  无边框窗口没有系统边框，边缘缩放热区得自己留。
    ///   GlassFrameThickness=-1   等价于老壳 <c>DwmExtendFrameIntoClientArea(-1,-1,-1,-1)</c>，
    ///                        否则 WebView2 的透明背景会糊成白底。
    ///   UseAeroCaptionButtons=false  页面右上角自己画了三个按钮，系统再画一份就成两套了。
    ///
    /// （想让系统画那三个按钮也不行：WindowChrome 的 Aero 按钮要把标题栏那片做成**非客户区**，
    ///   而非客户区里的 WebView2 内容是不可交互的 —— 顶栏上的密度选择、重新生成、导出
    ///   会一起变点不动。整窗都是 WebView2 的架构下这条路走不通，只能整条交给系统。）
    /// </summary>
    private void ApplyFramelessChrome()
    {
        WindowStyle = WindowStyle.None;
        WindowChrome.SetWindowChrome(this, new WindowChrome
        {
            CaptionHeight = 0,
            // 🔴 2026-09-22 第三轮：顶部不放大边界。
            //    原来四边都是 6px 缩放热区 ⇒ 标题栏最顶上 6px 是 NS-resize 箭头、还能真拖
            //    动窗口高度 —— 但那一片是标题栏，本应只能拖动（move），不是缩放（resize）。
            //    主人："鼠标放在顶栏和界面的交界处，它会出现调节界面的箭头，而且还真能调，
            //    是不是搞错了"。⇒ 顶部厚度设 0，缩放只留给左/右/底/四角；
            //    标题栏整片交给 #titlebar 的 mousedown→drag（index.html:889）。
            //    （页面那侧的 top 边缘也一并关掉，见 index.html 的 RZ_ALLOW_TOP。）
            ResizeBorderThickness = new Thickness(6, 0, 6, 6),
            GlassFrameThickness = new Thickness(-1),
            CornerRadius = new CornerRadius(0),
            UseAeroCaptionButtons = false,
        });
    }

    // ---------------------------------------------------------------- 窗口材质

    private static string? ReadPrefs(string key)
    {
        try
        {
            if (!File.Exists(Paths.UiPrefsFile)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(Paths.UiPrefsFile));
            return doc.RootElement.TryGetProperty(key, out var m) ? m.GetString() : null;
        }
        catch (Exception ex)
        {
            Log.Write($"读 ui-prefs.json 失败（{key}）: " + ex.Message);
            return null;
        }
    }

    /// <summary>
    /// 写回偏好。🔴 **必须合并**：老壳和我们都只有这一个文件，早先这里是
    /// "拿一个只装了 material 的字典直接覆盖" —— 一旦再加一个键（本次加了 appearance），
    /// 覆盖式写法就会把对方那个键**悄悄抹掉**（表现为"改了明暗、材质变回默认了"）。
    /// 所以先读回来、改掉这一个键、再整体写出去。
    /// </summary>
    private static void SavePrefs(string key, string value)
    {
        try
        {
            Directory.CreateDirectory(Paths.OutputDir);
            var bag = new Dictionary<string, object>();
            if (File.Exists(Paths.UiPrefsFile))
            {
                try
                {
                    using var doc = JsonDocument.Parse(File.ReadAllText(Paths.UiPrefsFile));
                    foreach (var p in doc.RootElement.EnumerateObject())
                        bag[p.Name] = p.Value.ValueKind == JsonValueKind.String
                            ? p.Value.GetString() ?? ""
                            : p.Value.GetRawText();
                }
                catch (Exception ex) { Log.Write("ui-prefs.json 解析失败，按空表重建: " + ex.Message); }
            }
            bag[key] = value;
            File.WriteAllText(Paths.UiPrefsFile, JsonSerializer.Serialize(bag));
            Log.Write($"偏好已保存：{key}={value} → {Paths.UiPrefsFile}");
        }
        catch (Exception ex)
        {
            Log.Write("保存偏好失败: " + ex.Message);
        }
    }

    private void OnSourceInitialized(object? sender, EventArgs e)
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        _hwnd = hwnd;
        Log.Write($"窗口句柄 hwnd=0x{hwnd:X}");

        Log.Write($"SetDark({(_appearance.Dark ? "dark" : "light")}) hr=0x{Dwm.SetDark(hwnd, _appearance.Dark):X8}");
        Log.Write($"SetRounded hr=0x{Dwm.SetRounded(hwnd, true):X8}");
        Log.Write($"ExtendFrame hr=0x{Dwm.ExtendFrameIntoClientArea(hwnd):X8}");

        ApplyMaterial(_material, save: false, notify: false);

        AuditFrameStyle(hwnd);

        // 挂窗过程序：目前干两件事 —— ① 让最大化按钮认得 Win11 的 Snap Layouts；
        // ② 记录 WM_NCHITTEST 到底有没有被系统问到（决定那条路通不通，不靠猜）。
        var hwndSrc = PresentationSource.FromVisual(this) as HwndSource;
        if (hwndSrc is not null)
        {
            // 🔴 关键一句：告诉 WPF 那一层"别画底"。
            //    DWM 的材质画在**窗口之后**，只要客户区被刷上不透明像素就全被盖住。
            //    frameless 模式下是 WPF 的 WindowChrome 悄悄替我们设了这一句
            //    （WindowChromeWorker 启用 GlassFrameThickness 时会把
            //     CompositionTarget.BackgroundColor 设成 Transparent）。
            //    换成系统原生标题栏后没有 WindowChrome 了，必须自己设 ——
            //    漏掉这句的症状就是：**材质只剩标题栏那一条还跟着变，窗口主体一片死黑**。
            //    （旁证：换回来之前，客户区在三档材质下量出来都是 (0,0,0)，
            //      设上之后才跟着档位走。）
            hwndSrc.CompositionTarget.BackgroundColor = System.Windows.Media.Colors.Transparent;
            Log.Write("✓ 已让 WPF 层不画底（CompositionTarget.BackgroundColor=Transparent）");

            if (!_wndProcHooked)
            {
                hwndSrc.AddHook(WndProc);
                _wndProcHooked = true;
                Log.Write("✓ 已挂窗口过程（WM_NCHITTEST / 状态广播）");
            }
        }
        else
        {
            Log.Write("⚠ 拿不到 HwndSource —— 透明底 / Snap Layouts / 命中测试这几条都走不通");
        }
    }

    /// <summary>
    /// 核对窗口样式位。这一步不做的话，"系统层面不许这个窗口最大化/缩放"这种问题
    /// 只会在用户按 Win+↑ 没反应时暴露出来，而且看不出原因。
    /// </summary>
    private void AuditFrameStyle(IntPtr hwnd)
    {
        var before = WindowShell.ReadStyle(hwnd);
        Log.Write("窗口样式 原样 | " + WindowShell.DescribeStyle(before));
        Log.Write("扩展样式 原样 | " + WindowShell.DescribeExStyle(WindowShell.ReadExStyle(hwnd)));

        if (WindowShell.EnsureFrameStyle(hwnd, out var b1, out var a1))
            Log.Write($"⚠ 窗口样式缺位已补 | {WindowShell.DescribeStyle(b1)} → {WindowShell.DescribeStyle(a1)}");
        else
            Log.Write("✓ 窗口样式已齐（SYSMENU / MINBOX / MAXBOX / THICKFRAME 都在，Win+方向键与 Alt+Space 可用）");

        if (WindowShell.EnsureTaskbarVisible(hwnd, out var b2, out var a2))
            Log.Write($"⚠ 曾被标成 TOOLWINDOW（任务栏会看不见），已摘掉 | {WindowShell.DescribeExStyle(b2)} → {WindowShell.DescribeExStyle(a2)}");

        if ((a1 & WindowShell.WsMaximizeBox) == 0)
            Log.Write("⚠ 窗口仍不可最大化 —— Win+↑ / 拖到屏幕顶部 Snap 会失灵");
        if ((a1 & WindowShell.WsThickFrame) == 0)
            Log.Write("⚠ 窗口仍不可缩放 —— Win+←/→ 分屏、拖到屏幕边缘 Snap 会失灵");
    }

    /// <summary>对应老壳 apply_material：DWM 背板与 WebView 底必须一起切。</summary>
    private void ApplyMaterial(MaterialSpec spec, bool save, bool notify)
    {
        _material = spec;
        ApplyLook();

        if (save) SavePrefs("material", spec.Name);
        if (notify) PostLook();
    }

    /// <summary>切深色 / 浅色。跟材质一样：DWM 那一套 + WebView 底 + 页面主题要一起切。</summary>
    private void ApplyAppearance(AppearanceSpec spec, bool save, bool notify)
    {
        _appearance = spec;
        ApplyLook();

        if (save) SavePrefs("appearance", spec.Name);
        if (notify) PostLook();

        // 页面上那一份要**立刻**跟着变（属性一改，CSS 就整片换掉）。
        // 顺序无所谓：PushThemeToPage 里先写 localStorage 再写属性，下一次导航也认。
        PushThemeToPage();
    }

    // ---------------------------------------------------------------- 材质降级（兼容 Win10）

    /// <summary>系统是否支持 DWM 系统材质（Mica/Acrylic/Tabbed，需 Win11 build 22000+）。缓存，避免每次读注册表。</summary>
    private static bool? _osSupportsBackdrop;
    private static bool OsSupportsBackdrop()
    {
        if (_osSupportsBackdrop.HasValue) return _osSupportsBackdrop.Value;
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
            var raw = (key?.GetValue("CurrentBuildNumber") as string)
                   ?? (key?.GetValue("CurrentBuild") as string);
            // 🔴 不读 Environment.OSVersion：本工程 app.manifest 只声明了 Win10 的 supportedOS，
            //    没声明 Win11 那条，Environment.OSVersion 在 Win11 上可能仍报 10.0.xxxx（失真）。
            //    读注册表 CurrentBuildNumber 才是真实 build，最稳。
            _osSupportsBackdrop = raw is not null && int.TryParse(raw, out var build) && build >= 22000;
        }
        catch (Exception ex)
        {
            Log.Write("读系统版本失败（按不支持系统材质处理）: " + ex.Message);
            _osSupportsBackdrop = false;
        }
        return _osSupportsBackdrop.Value;
    }

    /// <summary>
    /// 算出**实际要施到窗口**的材质：系统不支持系统材质时，把非实色档降级为实色（Solid）。
    /// 用户偏好（ui-prefs.json 里的 material）仍保留原选择，这里只决定"运行时生效的那一档"——
    /// 对应任务表第 36 条「Win10 亚克力降级，直接仅使用 solid」。
    /// </summary>
    private bool _backdropDowngradeWarned;
    private MaterialSpec EffectiveMaterial()
    {
        if (_material.Opaque || OsSupportsBackdrop()) return _material;
        if (!_backdropDowngradeWarned)
        {
            _backdropDowngradeWarned = true;
            Log.Write($"⚠ 当前系统不支持 {_material.Name} 材质（需 Windows 11 build 22000+），已自动降级为 solid");
        }
        return Materials.Solid;
    }

    /// <summary>
    /// 把当前「材质 + 明暗」一次施到窗口上。**唯一出口** —— 两个入口
    /// （ApplyMaterial / ApplyAppearance）都走它，避免出现"只切了一半"的组合：
    ///   ① DWM 背板材质（云母/亚克力/无）
    ///   ② DWM 明暗（深/浅：标题栏文字、系统右键菜单、滚动条都跟它走）
    ///   ③ 标题栏纯色（只有"实色"档要设，见下面的注释）
    ///   ④ WebView 底：材质档透明（透出材质）、实色档刷该主题的基底色
    /// </summary>
    private void ApplyLook()
    {
        var spec = EffectiveMaterial();
        var hwnd = new WindowInteropHelper(this).Handle;

        var hrDark = Dwm.SetDark(hwnd, _appearance.Dark);
        var hrBack = Dwm.SetBackdrop(hwnd, spec.Kind);

        // 🔴 实色档没有材质可画 ⇒ 标题栏落到"系统默认色"（深色模式下是**纯黑 #000000**），
        //    跟客户区的 #191919 之间就出现一条肉眼可见的台阶 —— 主人截图指的正是它。
        //    所以实色档显式把标题栏设成**客户区同一个底色**；
        //    有材质的档反过来还原成 Default：那里的标题栏是材质自己画的，
        //    设一块纯色反而会把材质盖住（又变成"标题栏和主体不搭"）。
        var hrCap = Dwm.SetCaptionColor(hwnd, spec.Opaque
            ? Dwm.ColorRef(_appearance.BaseHex)
            : Dwm.ColorDefault);

        SetOpaque(spec.Opaque);

        // caption= 这一项是**实测读数**，不是复述代码：实色档应当打出客户区那个底色，
        // 有材质的档应当打出 default（交还系统）。像素探针量的是"看起来对不对"，
        // 这一行量的是"我们到底发了什么"，两条对上了才敢说这个改动是通的。
        Log.Write($"apply_look(material={spec.Name} appearance={_appearance.Name} " +
                  $"base={_appearance.BaseHex} caption={(spec.Opaque ? _appearance.BaseHex : "default")}) " +
                  $"dark_hr=0x{hrDark:X8} backdrop_hr=0x{hrBack:X8} caption_hr=0x{hrCap:X8}" +
                  (hrBack != 0 ? "  ← 材质未生效（需 Win11 且系统开启透明效果）" : "") +
                  (hrCap != 0 ? "  ← 标题栏配色未生效（需 Win11 22000+）：不透明档的标题栏会退回系统默认色" : ""));
    }

    /// <summary>把当前组合广播给页面（material 消息带上 appearance，页面据此换配色）。
    /// 🔴 广播的是**实际生效**的材质（EffectiveMaterial），系统不支持时就是 solid；
    ///    这样前端卡片高亮和窗口真实表现一致，不会出现"选了亚克力却高亮亚克力、窗口其实是实色"。</summary>
    private void PostLook()
    {
        var spec = EffectiveMaterial();
        PostToWeb(new
        {
            type = "material",
            name = spec.Name,
            kind = spec.Kind,
            opaque = spec.Opaque,
            appearance = _appearance.Name,
        });
    }

    /// <summary>
    /// 页面后面那层底：材质档要透明（透出 DWM 材质），实色档刷**该主题的基底色**
    /// （对齐老壳 set_opaque；颜色从写死的 #191919 改成跟着明暗走，浅色是 #F3F3F3）。
    /// </summary>
    private void SetOpaque(bool opaque)
    {
        try
        {
            var (r, g, b) = Dwm.Rgb(_appearance.BaseHex);
            View.DefaultBackgroundColor = opaque
                ? System.Drawing.Color.FromArgb(255, r, g, b)
                : System.Drawing.Color.Transparent;
        }
        catch (Exception ex)
        {
            Log.Write("SetOpaque 失败: " + ex.Message);
        }
    }

    /// <summary>
    /// 把明暗推给页面。
    /// 🔴 为什么走 localStorage 而不是只发消息：页面是"先渲染、后收消息"的，
    ///    浅色模式下若按默认的深色配色画第一帧，就会出现**浅底上写浅字**的一瞬（看着像闪一下白）。
    ///    所以：宿主把值写进 localStorage，随文档注入的脚本在**页面脚本之前**读它并设好
    ///    data-theme（见 ThemeInjectJs）⇒ 第一帧就是对的。
    /// </summary>
    private void PushThemeToPage()
    {
        var name = _appearance.Name;
        _ = Eval($"(function(){{try{{localStorage.setItem('dsh.appearance',{JsonSerializer.Serialize(name)});}}catch(e){{}}" +
                 $"document.documentElement.setAttribute('data-theme',{JsonSerializer.Serialize(name)});" +
                 $"return document.documentElement.getAttribute('data-theme');}})()");
    }

    /// <summary>
    /// WebView2 环境参数：平时 null（全默认），只有设了 ADOFAI_SHELL_CDP 才开调试端口。
    ///
    /// 为什么需要它：壳里的页面跑在 WebView2 里，**从外面看不见 DOM**。以前要确认
    /// "这个坐标上到底是哪个元素""这条消息页面到底发没发"，只能靠改页面加日志再赌一把，
    /// 或者用 Supermium 另开一份"像但不是同一份"的页面去推测 —— 两者都会给出*看起来*
    /// 像结论的东西。开了这个口就能用 CDP 直接问壳里那一份，是"测的"和"给的"同一份字节。
    ///
    /// 🔴 默认关闭是故意的：调试端口等于给本机任意进程开一个"操控你这个窗口"的后门，
    ///    绝不能跟着正式版一起开着。
    /// </summary>
    private static CoreWebView2EnvironmentOptions? BuildEnvOptions()
    {
        var port = Environment.GetEnvironmentVariable("ADOFAI_SHELL_CDP");
        if (string.IsNullOrWhiteSpace(port)) return null;

        Log.Write($"⚠ 已开启 WebView2 调试端口 {port}（ADOFAI_SHELL_CDP）—— 仅开发期排查，正式版不要开");
        return new CoreWebView2EnvironmentOptions(
            additionalBrowserArguments: $"--remote-debugging-port={port} --remote-allow-origins=*");
    }

    // ---------------------------------------------------------------- 启动

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        try
        {
            Directory.CreateDirectory(Paths.WebViewCache);
            Log.Write("WebView2 缓存目录: " + Paths.WebViewCache);

            var env = await CoreWebView2Environment.CreateAsync(null, Paths.WebViewCache, BuildEnvOptions());
            await View.EnsureCoreWebView2Async(env);

            _core = View.CoreWebView2;
            _core.Settings.AreDefaultContextMenusEnabled = false;
            _core.Settings.IsStatusBarEnabled = false;
            _core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            _core.WebMessageReceived += OnWebMessage;
            _core.ProcessFailed += (_, args) => Log.Write("【WebView2 进程异常】" + args.ProcessFailedKind);

            // ★ 页面里的外链一律丢给系统默认浏览器（「关于」页的贡献者链接就是这么走的）。
            //   不加这两条的后果，两种写法各坏一种：
            //     ① <a target="_blank">   → WebView2 想开一个**自己的**新窗口，而 WPF 宿主
            //        没给它容器，表现为"点了没反应"或者蹦出一个没样式的裸窗口；
            //     ② <a href="https://…">  → 直接在**当前** WebView2 里导航，整个应用界面
            //        被顶成 github.com，用户回不来（只能杀进程）。
            //   ⇒ NewWindowRequested 收 target="_blank" / window.open；
            //     NavigationStarting 兜住漏写 target 的裸链接。两者都走 ShellOpen，
            //     UseShellExecute=true 交给系统按协议找默认浏览器。
            _core.NewWindowRequested += (_, e) =>
            {
                e.Handled = true;
                Log.Write("外链转交系统浏览器: " + e.Uri);
                ShellOpen(e.Uri, isFile: false);
            };

            _core.NavigationStarting += (_, e) =>
            {
                // 🔴 注意方向：IsInternalUrl 为 true 表示"是自己家的"⇒ **放行**。
                //    写成 `if (!IsInternalUrl(...)) return;` 就反了 —— 那会把
                //    http://127.0.0.1:8766/index.html 这个首导航也拦掉，应用直接白屏。
                if (IsInternalUrl(e.Uri)) return;
                e.Cancel = true;
                Log.Write("拦截外部导航、转交系统浏览器: " + e.Uri);
                ShellOpen(e.Uri, isFile: false);
            };

            // ★ 必须在**首次导航之前**做好这两件事：
            //   ① 开非客户区支持（文档明写「下次导航才生效」，晚一步就等于全程没开）；
            //   ② 注册随文档一起注入的脚本（给页面补 app-region 规则）。
            await SetupChromeScriptsAsync();

            // 每张页面加载完重新量一次最大化按钮的位置（换页/缩放后矩形会变）
            _core.NavigationCompleted += async (_, _) =>
            {
                await Task.Delay(600);
                await QueryMaxButtonRectAsync();
                PostWinState(force: true);
            };

            SetOpaque(_material.Opaque);
            Log.Write("WebView2 就绪 " + _core.Environment.BrowserVersionString);

            // ① 先把网关拉起来 —— 页面必须经网关访问，file:// 下 ES module 会被 CORS 拦死
            try
            {
                _gateway.Start(Paths.GatewayPort);
            }
            catch (Exception ex)
            {
                Log.Write("【网关启动失败】" + ex.Message);
                ShowFatal("同源网关启动失败", ex);
                return;
            }

            // ② 再（后台）把Adofai-Chart-Generator引擎拉起来，别挡着窗口显示
            Sidecar.StartInBackground();

            // ③ 导航：调试入口 ADOFAI_SHELL_URL 可覆盖（指向 file:// 或隔离端口）
            var target = Environment.GetEnvironmentVariable("ADOFAI_SHELL_URL");
            Navigate(string.IsNullOrWhiteSpace(target) ? $"http://127.0.0.1:{_gateway.Port}/index.html" : target);

            if (Environment.GetEnvironmentVariable("ADOFAI_SHELL_SELFTEST") == "1")
                _ = RunSelfTestAsync();
        }
        catch (Exception ex)
        {
            ShowFatal("WebView2 初始化失败", ex);
        }
    }

    private void Navigate(string url)
    {
        try
        {
            _core?.Navigate(url);
            Log.Write("Navigate: " + url);
        }
        catch (Exception ex)
        {
            ShowFatal("导航失败：" + url, ex);
        }
    }

    private void NavigateImport() => Navigate($"http://127.0.0.1:{_gateway.Port}/index.html");

    private void NavigateWorkbench()
    {
        var url = $"http://127.0.0.1:{_gateway.Port}/workbench/index.html";
        // 手动点进来时若没有待载入谱面，自动载入「最近一次生成的 MIDI」——
        // 否则工作台是个空壳（左栏没文件、没音轨，中间没谱面），
        // 用户会以为「界面上的东西全没了」（老壳 2026-09-19 实测就是这个现象）。
        if (string.IsNullOrEmpty(_pendingStudioLoad))
        {
            var auto = HostApi.LatestCombinedMidi();
            if (auto is not null && File.Exists(auto))
            {
                _pendingStudioLoad = auto;
                Log.Write("open_workbench: 无待载入谱面，自动载入最近生成 " + auto);
            }
            else
            {
                Log.Write("open_workbench: 无待载入谱面，也没有可自动载入的 MIDI（空工作台）");
            }
        }
        Navigate(url);
    }

    private void ShowFatal(string title, Exception ex)
    {
        Log.Write($"【致命】{title}: {ex}");
        FatalText.Text = title + "\n\n" + ex.Message + "\n\n日志：" + Log.File;
        Fatal.Visibility = Visibility.Visible;
    }

    // ---------------------------------------------------------------- 桥：页面 → 宿主

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        JsonElement root;
        try
        {
            // 前端桥可能发对象，也可能发 JSON 字符串 —— 两种都得认
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            root = doc.RootElement.Clone();
            if (root.ValueKind == JsonValueKind.String)
            {
                using var inner = JsonDocument.Parse(root.GetString() ?? "null");
                root = inner.RootElement.Clone();
            }
        }
        catch (Exception ex)
        {
            Log.Write("收到无法解析的消息: " + ex.Message);
            return;
        }

        var type = root.TryGetProperty("type", out var t) ? t.GetString() ?? "" : "";
        // 排障第一现场：每条进来的消息都留痕。别为了"日志干净"删掉它 ——
        // 删了以后就只能靠猜"到底是宿主没答还是页面没渲染"（本轮真踩过这个坑）。
        // 页面可以带一个 dbg 字段（如"这条消息是谁触发的"），有就一起打出来 ——
        // 排查"点在按钮上结果触发的是标题栏"这类问题时，没有它就只剩猜。
        var dbg = root.TryGetProperty("dbg", out var dv) ? dv.ToString() : "";
        Log.Write("← 页面: " + type + (string.IsNullOrEmpty(dbg) ? "" : "  (" + dbg + ")"));

        try
        {
            Dispatch(type, root);
        }
        catch (Exception ex)
        {
            Log.Write($"处理 {type} 出错: {ex}");
        }
    }

    private void Dispatch(string type, JsonElement msg)
    {
        switch (type)
        {
            // ---------- 窗口控制（页面自绘标题栏，宿主只做系统动作）----------
            case "drag":
                BeginSystemMove(HtCaption);
                break;

            case "resize":
                if (WindowState != WindowState.Maximized)
                {
                    var edge = msg.TryGetProperty("edge", out var ed) ? ed.GetString() ?? "" : "";
                    if (EdgeHt.TryGetValue(edge, out var ht))
                        BeginSystemMove(ht);
                }
                break;

            case "winminimize":
                WindowState = WindowState.Minimized;
                break;

            case "winmaximize":
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                // 不在这里发 winstate 了 —— StateChanged 是唯一出口，避免两处各发一次
                break;

            case "close":
                Close();
                break;

            // 右键标题栏 → 弹系统原生菜单（页面负责报"用户右键了"，菜单本身是系统的）
            case "sysmenu":
                ShowSystemMenu();
                break;

            // ---------- 页面 ↔ 宿主数据 ----------
            case "get_schema":
                // 前端用它当「页面已就绪」的信号。老壳这里会补发积压消息 + 重施材质：
                // ★ 启动时 WebView 还没建、set_opaque 会静默跳过 ⇒ 选了「实色」重启会变回透明，
                //   必须在这里补施一次（踩过这个坑）。
                Log.Write("✓ 页面已就绪（收到 get_schema）");
                ApplyMaterial(_material, save: false, notify: false);
                // 顺带把主题也写一遍：① 首次运行 localStorage 还是空的，得种下去，
                //    下一次导航（含进工作台）才能靠注入脚本在首帧就摆正；
                // ② 万一注入那一路没跑成，这里也能把当前页的属性纠回来。
                PushThemeToPage();
                PostSchema();
                if (Environment.GetEnvironmentVariable("ADOFAI_SHELL_SELFTEST") == "1")
                {
                    _ = RunSelfTestAsync();
                    _ = RunHostWalkAsync();
                }
                break;

            case "selftest_pong":
                Log.Write("✓ 自检通过：宿主→页面→宿主 双向通道都通 | " + msg.GetRawText());
                break;

            case "appinfo":
                PostAppInfo();
                break;

            case "setmaterial":
                ApplyMaterial(Materials.Resolve(msg.TryGetProperty("name", out var mn) ? mn.GetString() : null),
                              save: true, notify: true);
                break;

            // 深色 / 浅色（2026-09-22 新增）。只给「云母 / 实色」两档在卡片上挂选择器。
            case "setappearance":
                ApplyAppearance(Appearances.Resolve(msg.TryGetProperty("name", out var an) ? an.GetString() : null),
                                save: true, notify: true);
                break;

            case "setbackdrop":
                {
                    var mat = msg.TryGetProperty("mat", out var mv) && mv.TryGetInt32(out var mvInt) ? mvInt : 2;
                    Log.Write($"setbackdrop mat={mat} hr=0x{Dwm.SetBackdrop(new WindowInteropHelper(this).Handle, mat):X8}");
                    break;
                }

            case "setopaque":
                SetOpaque(msg.TryGetProperty("on", out var op) && op.ValueKind == JsonValueKind.True);
                break;

            case "pickfile":
                PickFile(msg.TryGetProperty("dest", out var dst) ? dst.GetString() ?? "" : "");
                break;

            case "listhistory":
                PostToWeb(HostApi.History());
                break;

            case "listmidis":
                PostToWeb(HostApi.Midis());
                break;

            case "liststems":
                PostToWeb(HostApi.Stems());
                break;

            case "getexplain":
                PostToWeb(HostApi.Explain());
                break;

            case "mkexplain":
                PostToWeb(HostApi.MkExplain(msg.TryGetProperty("force", out var f) && f.ValueKind == JsonValueKind.True));
                break;

            case "traininfo":
                PostToWeb(HostApi.TrainInfo());
                break;

            case "cleanwork":
                PostToWeb(HostApi.CleanWork());
                break;

            case "openfile":
                ShellOpen(msg.TryGetProperty("path", out var op1) ? op1.GetString() : null, isFile: true);
                break;

            case "openfolder":
                {
                    // 🔴 这里必须是 isFile:false —— ShellOpen 里 `if (isFile && !File.Exists(path)) return;`
                    //    那条守卫是给「打开某个文件」用的。目录不是文件，File.Exists(目录) 恒为 false，
                    //    所以传 isFile:true 会在守卫处直接 return ⇒「打开目录」点了毫无反应（主人 2026-09-22 报的 bug）。
                    //    isFile:false 时跳过守卫，Process.Start(目录, UseShellExecute) 由系统用资源管理器打开它。
                    var folder = msg.TryGetProperty("folder", out var fo) ? fo.GetString() : null;
                    if (!string.IsNullOrEmpty(folder) && Directory.Exists(folder)) ShellOpen(folder, isFile: false);
                    else
                    {
                        var p = msg.TryGetProperty("path", out var pa) ? pa.GetString() : null;
                        if (!string.IsNullOrEmpty(p))
                        {
                            var dir = Path.GetDirectoryName(p);
                            if (dir is not null && Directory.Exists(dir)) ShellOpen(dir, isFile: false);
                        }
                    }
                    break;
                }

            case "deletehist":
                {
                    var p = msg.TryGetProperty("path", out var dp) ? dp.GetString() : null;
                    if (!string.IsNullOrEmpty(p) && File.Exists(p))
                    {
                        try { File.Delete(p); }
                        catch (Exception ex) { Log.Write("deletehist 失败: " + ex.Message); }
                        PostToWeb(HostApi.History());
                    }
                    break;
                }

            // ---------- 流水线与后端 ----------
            case "backend_start":
                Sidecar.StartInBackground();
                break;

            case "backend_status":
                PostToWeb(new { type = "backend_status", ok = Sidecar.IsHealthy(), port = Paths.SidecarPort });
                break;

            case "separate_launch":
                LaunchSeparateGui();
                break;

            // ---------- 页面切换 ----------
            case "nav_studio":
            case "open_workbench":
                NavigateWorkbench();
                break;

            case "open_in_workbench":
                {
                    var midi = msg.TryGetProperty("path", out var mip) ? mip.GetString() : null;
                    if (!string.IsNullOrEmpty(midi) && File.Exists(midi)) _pendingStudioLoad = midi;
                    NavigateWorkbench();
                    break;
                }

            // ---------- 工作台（dsh 桥）----------
            case "dsh_call":
                HandleDshCall(msg);
                break;

            case "dsh_ready":
                Log.Write("dsh_ready: " + (msg.TryGetProperty("info", out var inf) ? inf.GetRawText() : ""));
                MaybeAutoLoadStudio();
                break;

            // ---------- 日志回传（排障第一现场）----------
            case "workbench_status":
                Log.Write("WORKBENCH_STATUS " + (msg.TryGetProperty("status", out var st) ? st.GetRawText() : ""));
                break;

            case "page_log":
                Log.Write("PAGE_LOG " + (msg.TryGetProperty("step", out var step) ? step.GetRawText() : ""));
                break;

            case "page_error":
                {
                    // ★ 必须带 src/line/col：只打 message 的话根本定位不到是哪个文件哪一行
                    var src = msg.TryGetProperty("src", out var s) ? s.GetString() ?? "" : "";
                    if (src.Length > 0) src = Path.GetFileName(src);
                    Log.Write(string.Format("PAGE_ERROR {0} @{1}:{2}:{3}",
                        msg.TryGetProperty("msg", out var m) ? m.GetString() : "",
                        src,
                        msg.TryGetProperty("line", out var ln) ? ln.ToString() : "",
                        msg.TryGetProperty("col", out var cl) ? cl.ToString() : ""));
                    break;
                }

            case "dsh_error":
                Log.Write("dsh_error: " + (msg.TryGetProperty("msg", out var dm) ? dm.GetString() : ""));
                break;

            // ---------- 流水线重活 ----------
            case "generate":
                RunGenerate(msg);
                break;
            case "tune":
            case "save":
                NotYetWired(type);
                break;

            default:
                Log.Write($"（尚未实现的消息：{type}）");
                break;
        }
    }

    private void NotYetWired(string type)
    {
        const string detail =
            "这是 C# 新壳（第 3 阶段进行中）。窗口、网关、工作台、参数微调、导出预览都已经过 C# 跑，" +
            "但「音频 → 分离 → 转谱 → 出谱」这条重活还在 Python 老壳里。\n\n" +
            "两条路：\n" +
            "  · 想现在出谱 → 双击项目根目录的「启动.bat」（老壳，功能全）\n" +
            "  · 想推进新壳 → 这条链路是下一步要搬的（配合 ONNX 化一起做）";
        Log.Write($"【未接通】{type} —— 已如实告知用户，没有假装执行");
        PostToWeb(new { type = "gen_log", line = "[warn] 当前是 C# 新壳，生成链路尚未接通（第 3 阶段进行中）。" });
        PostToWeb(new { type = "gen_log", line = "[warn] 想现在出谱请用「启动.bat」跑老壳。" });
        PostToWeb(new { type = "generated", ok = false, msg = "生成链路在 C# 新壳里还没接通", detail });
    }

    /// <summary>工作台就绪后，把待载入的 MIDI 灌进页面（对齐老壳 _maybe_auto_load_studio）。</summary>
    private void MaybeAutoLoadStudio()
    {
        var path = _pendingStudioLoad;
        if (string.IsNullOrEmpty(path)) return;
        _pendingStudioLoad = null;
        if (!File.Exists(path))
        {
            Log.Write("auto_load_studio: MIDI 不存在，跳过: " + path);
            return;
        }
        Log.Write("auto_load_studio: " + path);
        var js = $"try{{ if(window.__dsh && window.__dsh.load) window.__dsh.load({JsonSerializer.Serialize(path)}); }}catch(e){{}}";
        _ = _core?.ExecuteScriptAsync(js);
    }

    // ---------------------------------------------------------------- dsh 桥

    private static readonly Dictionary<string, int> EdgeHt = new()
    {
        ["left"] = 10, ["right"] = 11, ["top"] = 12, ["bottom"] = 15,
        ["topleft"] = 13, ["topright"] = 14, ["bottomleft"] = 16, ["bottomright"] = 17,
    };

    private const int HtCaption = 2;

    private int _rectQueryToken;

    private void HandleDshCall(JsonElement msg)
    {
        var cid = msg.TryGetProperty("id", out var idEl) && idEl.TryGetInt32(out var id) ? id : 0;
        var name = msg.TryGetProperty("name", out var nm) ? nm.GetString() ?? "" : "";
        var args = msg.TryGetProperty("args", out var ag) && ag.ValueKind == JsonValueKind.Array
            ? ag.EnumerateArray().ToArray()
            : Array.Empty<JsonElement>();

        try
        {
            object? result = name switch
            {
                "openFile" => PickProjectFile(),
                "openAudio" => PickAudioFile(),
                "openDir" => PickFolder(ArgString(args, 0, "title")),
                "reveal" => RevealInExplorer(ArgString(args, 0) ?? ""),
                "info" => new { version = "0.4.6", port = Paths.SidecarPort, electron = "ADOFAI Studio / WebView2" },
                "layoutGet" => ReadUiLayout(),
                "layoutSet" => WriteUiLayout(ArgString(args, 0) ?? ""),
                "backToImport" => BackToImport(),
                _ => throw new InvalidOperationException("未知 dsh 调用: " + name),
            };
            PostToWeb(new { type = "dsh_reply", id = cid, ok = true, result, error = (string?)null });
        }
        catch (Exception ex)
        {
            Log.Write($"dsh_call({name}) 失败: {ex.Message}");
            PostToWeb(new { type = "dsh_reply", id = cid, ok = false, result = (object?)null, error = ex.Message });
        }
    }

    private static string? ArgString(JsonElement[] args, int idx, string? key = null)
    {
        if (idx >= args.Length) return null;
        var a = args[idx];
        if (key is null) return a.ValueKind == JsonValueKind.String ? a.GetString() : null;
        return a.ValueKind == JsonValueKind.Object && a.TryGetProperty(key, out var v) ? v.GetString() : null;
    }

    private object BackToImport()
    {
        NavigateImport();
        return "";
    }

    private static string ReadUiLayout()
    {
        try { return File.Exists(Paths.UiLayoutFile) ? File.ReadAllText(Paths.UiLayoutFile) : ""; }
        catch (Exception ex) { Log.Write("layoutGet 失败: " + ex.Message); return ""; }
    }

    private static string WriteUiLayout(string text)
    {
        try { File.WriteAllText(Paths.UiLayoutFile, text ?? ""); }
        catch (Exception ex) { Log.Write("layoutSet 失败: " + ex.Message); }
        return "";
    }

    private static string RevealInExplorer(string path)
    {
        if (path.Length == 0 || (!File.Exists(path) && !Directory.Exists(path))) return "";
        try
        {
            // explorer /select 要显示窗口，不能隐藏
            Process.Start(new ProcessStartInfo("explorer", $"/select,\"{path}\"") { UseShellExecute = true });
        }
        catch (Exception ex) { Log.Write("reveal 失败: " + ex.Message); }
        return "";
    }

    // ---------------------------------------------------------------- 文件对话框

    /// <summary>老壳的 SRC_FILTER / AUDIO_FILTER，一字不差。</summary>
    private const string SrcFilter =
        "谱面工程|*.mid;*.midi;*.json;*.bdg|时间戳JSON|*_timestamps.json|MIDI|*.mid;*.midi|所有文件|*.*";
    private const string AudioFilter =
        "音频文件|*.mp3;*.wav;*.ogg;*.flac;*.m4a;*.aac|所有文件|*.*";

    private string? PickProjectFile() => ShowOpen(SrcFilter, "选择谱面工程（MIDI / 时间戳JSON / BDG）");
    private string? PickAudioFile() => ShowOpen(AudioFilter, "选择音频文件（用于采音 / 预览音源）");

    private string? ShowOpen(string filter, string title)
    {
        var dlg = new OpenFileDialog
        {
            Filter = filter,
            Title = title,
            CheckFileExists = true,
            CheckPathExists = true,
        };
        return dlg.ShowDialog(this) == true ? dlg.FileName : null;
    }

    private string? PickFolder(string? title)
    {
        var dlg = new OpenFolderDialog
        {
            Title = title ?? "选择导出目录（会新建一个曲目文件夹）",
            Multiselect = false,
        };
        return dlg.ShowDialog(this) == true ? dlg.FolderName : null;
    }

    /// <summary>pickfile 消息（导入页拖拽区点击选音乐）。dest=audio 用音频过滤器，
    /// 其余按谱面工程过滤器 —— 老壳一律用谱面过滤器，那是它的一个瑕疵，这里按语义走。</summary>
    private void PickFile(string dest)
    {
        var path = dest == "audio"
            ? PickAudioFile()
            : PickProjectFile();
        if (!string.IsNullOrEmpty(path))
            PostToWeb(new { type = "filepath", path, dest });
    }

    /// <summary>拉起 audio-sep 独立分离界面（对齐老壳 do_separate_launch）。</summary>
    private void LaunchSeparateGui()
    {
        var py = Paths.PythonExe;
        var gui = Path.Combine(Paths.AudioSepDir, "sep_gui.py");
        if (!File.Exists(py) || !File.Exists(gui))
        {
            PostToWeb(new { type = "separate_launched", ok = false, msg = "独立分离窗口已下线；分离会在「生成谱面」时自动完成" });
            return;
        }
        try
        {
            var psi = new ProcessStartInfo(py, "\"" + gui + "\"")
            {
                WorkingDirectory = Path.Combine(Paths.StudioRoot, "audio-sep"),
                UseShellExecute = false,
                CreateNoWindow = true,   // 不带这个会闪一个控制台黑框
            };
            Process.Start(psi);
            PostToWeb(new
            {
                type = "separate_launched",
                ok = true,
                msg = "已拉起 audio-sep 分离界面（独立窗口），分离后把产物喂回本工具",
            });
        }
        catch (Exception ex)
        {
            PostToWeb(new { type = "separate_launched", ok = false, msg = "拉起失败：" + ex.Message });
        }
    }

    // ---------------------------------------------------------------- 生成流水线
    /// <summary>
    /// C# 新壳接通「音频 → 分离 → 转谱 → 出谱」整条链路（第 14 条 + 接通用）。
    ///
    /// 不重写任何引擎逻辑：spawn gui/python313/python.exe 跑 gui/gen_cli.py，
    /// 它复用老壳同一份 backend.generate()，把进度以「一行一条 JSON」打到 stdout，
    /// 这里逐行解析回传页面（gen_stage / gen_log / generated）。
    /// 完成后把 MIDI 设为待载入并切到工作台（等价于老壳 post nav_studio）。
    ///
    /// ⚠️ sidecar 由本程序自己托管（端口一致），gen_cli 用 --port 显式指向它、
    ///   且只在「侧未健康」时才 start()，否则会误杀本程序托管的 sidecar、
    ///   把工作台网关打挂。
    /// </summary>
    private void RunGenerate(JsonElement msg)
    {
        Log.Write("【生成链路已接通】C# → gen_cli.py → backend.generate（第 14 条 + 接通用）");
        Log.Write("【OSN1 已接通】生成页采点方式新增 OSN1（自训练模型），C# 透传 --mode osn1 给 gen_cli");
        Log.Write("【生成编码已钉死】gen_cli 子进程强制 UTF-8（PYTHONUTF8 / PYTHONIOENCODING），"
                  + "stderr 已接住并落盘 —— 中文不再变方块，也不再半路静默死");
        var audio = msg.TryGetProperty("audio", out var a) ? a.GetString() : null;
        var size = msg.TryGetProperty("size", out var s) ? s.GetString() : "medium";
        var bpm = msg.TryGetProperty("bpm", out var b) && b.ValueKind == JsonValueKind.Number
            ? b.GetDouble() : (double?)null;
        if (bpm is <= 0) bpm = null;
        var mode = msg.TryGetProperty("mode", out var mo) ? mo.GetString() : "muscriptor";
        if (mode != "osn1") mode = "muscriptor";
        if (string.IsNullOrEmpty(audio) || !File.Exists(audio))
        {
            PostToWeb(new { type = "generated", ok = false, msg = "请先选择有效的音乐文件" });
            return;
        }
        if (size != "small" && size != "medium" && size != "large") size = "medium";

        // 与 LaunchSeparateGui 同款解释器（项目约定的 python313 在本快照里对应的是
        // audio-sep/runtime/python.exe；gen_cli 只需 stdlib，能 import backend 即可）。
        var py = Paths.PythonExe;
        var script = Path.Combine(Paths.GuiDir, "gen_cli.py");
        if (!File.Exists(py) || !File.Exists(script))
        {
            PostToWeb(new { type = "generated", ok = false, msg = "找不到生成引擎：" + py });
            return;
        }

        var args = new StringBuilder()
            .Append('"').Append(script).Append("\" ")
            .Append('"').Append(audio).Append("\" ")
            .Append(size);
        if (bpm.HasValue)
            args.Append(" --bpm ").Append(bpm.Value.ToString(System.Globalization.CultureInfo.InvariantCulture));
        if (mode == "osn1")
            args.Append(" --mode osn1");
        args.Append(" --port ").Append(Paths.SidecarPort);

        var psi = new ProcessStartInfo(py, args.ToString())
        {
            WorkingDirectory = Paths.StudioRoot,   // 与老壳一致：backend 的相对路径才对
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            // ★ stderr 必须接住：Python 的报错（尤其 UnicodeEncodeError 那种会**直接打死
            //   桥进程**的）全在这里。2026-09-25 真机事故就是没接它 —— 界面只显示
            //   "进程没了"，日志里一个字都没有，一次复现全白瞎。
            RedirectStandardError = true,
            StandardOutputEncoding = new System.Text.UTF8Encoding(false),
            StandardErrorEncoding = new System.Text.UTF8Encoding(false),
        };
        // ★ 与 gui/gen_cli.py 的 _force_utf8() 双保险：先把子进程编码钉成 UTF-8。
        //   Windows 上 Python 的 stdout **一被重定向**就退回 ANSI 代码页（本机 cp936），
        //   而这边按 UTF-8 解码 ⇒ 中文变方块；更狠的是 cp936 里没有 `⚠`(U+26A0)，
        //   转谱出空轨时那句 `⚠ 几乎为空` 日志一 encode 就把桥进程打崩。
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        bool seenGenerated = false;
        Process? proc = null;
        var errTail = new System.Collections.Generic.Queue<string>();   // 留最后几行 stderr 当"死因"
        try
        {
            proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            proc.OutputDataReceived += (_, e) =>
            {
                if (string.IsNullOrEmpty(e.Data)) return;
                HandleGenCliLine(e.Data, ref seenGenerated);
            };
            proc.ErrorDataReceived += (_, e) =>
            {
                if (string.IsNullOrEmpty(e.Data)) return;
                lock (errTail)
                {
                    errTail.Enqueue(e.Data);
                    while (errTail.Count > 6) errTail.Dequeue();
                }
                Log.Write("[gen_cli:err] " + e.Data);
            };
            if (!proc.Start())
            {
                PostToWeb(new { type = "generated", ok = false, msg = "启动生成进程失败" });
                return;
            }
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();

            // 后台等结束：若进程退出却没收到 generated 行（极端崩溃），补一条失败，
            // 并把退出码 + stderr 尾巴一起报上来 —— 别再让"为什么死的"无从查起。
            _ = Task.Run(() =>
            {
                var exitCode = -1;
                try { proc.WaitForExit(); exitCode = proc.ExitCode; } catch { }
                System.Threading.Thread.Sleep(200);   // 给异步输出管道一拍收尾
                if (!seenGenerated)
                {
                    string tail;
                    lock (errTail) tail = string.Join(" ｜ ", errTail);
                    Log.Write("✗ 生成进程异常退出 exit=" + exitCode
                              + (tail.Length > 0 ? "，stderr 末尾：" + tail : "（stderr 无输出）"));
                    PostToWeb(new
                    {
                        type = "generated",
                        ok = false,
                        msg = "生成进程异常退出（exit=" + exitCode + "，没产出结果）"
                              + (tail.Length > 0 ? "：" + tail : "，详见程序日志"),
                    });
                }
                try { proc.Dispose(); } catch { }
            });
        }
        catch (Exception ex)
        {
            PostToWeb(new { type = "generated", ok = false, msg = "拉起生成失败：" + ex.Message });
            try { proc?.Dispose(); } catch { }
        }
    }

    private void HandleGenCliLine(string line, ref bool seenGenerated)
    {
        JsonDocument? doc = null;
        try { doc = JsonDocument.Parse(line); }
        catch { return; }
        using (doc)
        {
            var root = doc.RootElement;
            var type = root.TryGetProperty("type", out var t) ? t.GetString() : "";
            switch (type)
            {
                case "gen_stage":
                    Log.Write("[gen] 阶段 "
                              + (root.TryGetProperty("n", out var nEl) ? nEl.GetInt32() : 0)
                              + " / " + (root.TryGetProperty("name", out var nmEl) ? nmEl.GetString() : "")
                              + " " + (root.TryGetProperty("detail", out var dtEl) ? dtEl.GetString() : ""));
                    PostToWeb(new
                    {
                        type = "gen_stage",
                        n = root.TryGetProperty("n", out var n) ? n.GetInt32() : 0,
                        name = root.TryGetProperty("name", out var nm) ? nm.GetString() ?? "" : "",
                        detail = root.TryGetProperty("detail", out var dt) ? dt.GetString() ?? "" : "",
                    });
                    break;
                case "gen_log":
                    var logLine = root.TryGetProperty("line", out var l) ? l.GetString() ?? "" : "";
                    Log.Write("[gen] " + logLine);
                    PostToWeb(new { type = "gen_log", line = logLine });
                    break;
                case "generated":
                    seenGenerated = true;
                    Log.Write("[gen] 收尾 " + (line.Length > 400 ? line.Substring(0, 400) + " …" : line));
                    var ok = root.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;
                    if (ok)
                    {
                        var midi = root.TryGetProperty("midi", out var m) ? m.GetString() : null;
                        var stems = root.TryGetProperty("stems", out var st) && st.ValueKind == JsonValueKind.Number
                            ? st.GetInt32() : 0;
                        var failed = root.TryGetProperty("failed", out var fl) && fl.ValueKind == JsonValueKind.Array
                            ? fl.EnumerateArray().Select(x => x.GetString() ?? "").ToArray()
                            : Array.Empty<string>();
                        if (!string.IsNullOrEmpty(midi) && File.Exists(midi))
                            _pendingStudioLoad = midi;
                        PostToWeb(new { type = "generated", ok = true, midi, stems, failed });
                        // 切到工作台并自动载入（等价于老壳 post nav_studio）
                        if (Dispatcher.CheckAccess()) NavigateWorkbench();
                        else Dispatcher.Invoke(NavigateWorkbench);
                    }
                    else
                    {
                        var m = root.TryGetProperty("msg", out var mg) ? mg.GetString() ?? "未知错误" : "未知错误";
                        PostToWeb(new { type = "generated", ok = false, msg = m });
                    }
                    break;
            }
        }
    }

    /// <summary>
    /// 这个地址是不是"我们自己家的"？不是的话，它就是页面里的外链，
    /// NavigationStarting 会取消这次导航并交给系统浏览器。
    /// 判定刻意放宽到"任何 127.0.0.1 / localhost 端口"：调试时用 ADOFAI_SHELL_URL
    /// 指到别的本地端口，也不会被自己拦下来。
    /// 认不出来的 scheme 一律当内部放行 —— 宁可漏拦，也不能把正常导航误杀。
    /// </summary>
    private static bool IsInternalUrl(string uri)
    {
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var u)) return true;
        switch (u.Scheme)
        {
            case "http":
            case "https":
                var h = u.Host;
                return h == "127.0.0.1" || h == "localhost" || h == "::1" || h == "[::1]";
            default:
                return true;   // file / data / about / blob / devtools 等本地资源
        }
    }

    private static void ShellOpen(string? path, bool isFile)
    {
        if (string.IsNullOrEmpty(path)) return;
        if (isFile && !File.Exists(path)) return;
        try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
        catch (Exception ex) { Log.Write("打开失败 " + path + ": " + ex.Message); }
    }

    // ---------------------------------------------------------------- 系统缩放/拖动

    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out PointStruct p);

    [StructLayout(LayoutKind.Sequential)]
    private struct PointStruct { public int X; public int Y; }

    private const int WmNcLButtonDown = 0x00A1;

    /// <summary>
    /// 交给系统原生缩放/拖动循环 —— 与老壳同一条路（ReleaseCapture + WM_NCLBUTTONDOWN）。
    /// 页面铺满客户区，父窗口拿不到 WM_NCHITTEST，只能由页面自报边缘。
    /// </summary>
    private void BeginSystemMove(int hitTest)
    {
        try
        {
            var hwnd = new WindowInteropHelper(this).Handle;
            GetCursorPos(out var pt);
            var lp = (IntPtr)((pt.Y << 16) | (pt.X & 0xFFFF));
            ReleaseCapture();
            SendMessageW(hwnd, WmNcLButtonDown, (IntPtr)hitTest, lp);
        }
        catch (Exception ex)
        {
            Log.Write("BeginSystemMove 失败: " + ex.Message);
        }
    }

    // ---------------------------------------------------------------- 系统集成

    /// <summary>
    /// 给页面补 app-region 规则 —— 把"标题栏那条横条"变成系统眼里的真标题栏。
    ///
    /// 只有开了非客户区支持这货才生效（否则 app-region 被当普通 CSS 忽略）。
    /// 两个必须留意的点：
    ///   ① 所有可交互元素必须显式 no-drag，否则按钮点不动（这是最容易翻车的地方，
    ///      所以除了 button/input 这类，还带上 [onclick] 与 #win-ctl / #btn-layout 这些白名单）；
    ///   ② **顶边 8px 必须留个洞**（no-drag）—— 页面自己的缩放 JS 靠那几像素的
    ///      mousedown，被系统当标题栏吃掉的话，窗口上边缘就再也拉不动了。
    /// 页面里后来才建出来的元素（工作台的 #top）靠轮询兜住。
    /// </summary>
    private const string NcInjectJs = """
(function () {
  if (window.__shellNc) { return; }
  window.__shellNc = true;

  var RULE_DRAG = ' app-region: drag; -webkit-app-region: drag; ';
  var RULE_NODRAG = ' app-region: no-drag; -webkit-app-region: no-drag; ';
  var HIT = 'button,input,select,textarea,a,label,[contenteditable],[onclick],[role=button],[role=tab]';
  var EXTRA = '#win-ctl,#btn-layout,#btn-reset-layout,#btn-back-import';

  function report(obj) {
    try { window.chrome.webview.postMessage(JSON.stringify(obj)); } catch (e) { }
  }

  function bars() {
    var out = [];
    var tb = document.getElementById('titlebar');
    if (tb) { out.push(tb); }
    var top = document.getElementById('top');
    if (top) { out.push(top); }
    return out;
  }

  // 给逗号分开的选择器逐个加前缀（"#top button,#top input"），顺便把后代也带上。
  // 🔴 不逐条加前缀的话，"#top #win-ctl,#btn-layout" 里只有第一条被限定在 #top 内，
  //    剩下几条会变成全局选择器 —— 看着生效了，其实管不到该管的地方。
  function scoped(prefix, list) {
    return list.split(',').map(function (s) { return prefix + ' ' + s; }).join(',');
  }
  function scopedDeep(prefix, list) {
    return list.split(',').map(function (s) { return prefix + ' ' + s + ' *'; }).join(',');
  }

  function apply() {
    var bs = bars();
    if (!bs.length) { return false; }

    var css = '';
    var i, id;
    for (i = 0; i < bs.length; i++) {
      id = '#' + bs[i].id;
      css += id + '{' + RULE_DRAG + '}\n';
      css += scoped(id, HIT) + ',' + scoped(id, EXTRA) + ',' + scopedDeep(id, EXTRA)
           + '{' + RULE_NODRAG + '}\n';
    }
    css += '#__shell_topgrip{position:absolute;left:0;right:0;top:0;height:8px;'
         + RULE_NODRAG + '}\n';

    var style = document.createElement('style');
    style.id = '__shell_nc';
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);

    for (i = 0; i < bs.length; i++) {
      if (getComputedStyle(bs[i]).position === 'static') {
        bs[i].style.position = 'relative';
      }
      if (!bs[i].querySelector('#__shell_topgrip')) {
        var grip = document.createElement('div');
        grip.id = '__shell_topgrip';
        bs[i].insertBefore(grip, bs[i].firstChild);
      }
    }
    audit(bs);
    // 再隔一会儿补查一次：工作台那类页面的顶栏按钮是页面脚本后来才建出来的，
    // DOMContentLoaded 那一刻还没齐 —— 只查一次会把漏标的按钮放过去。
    setTimeout(function () { audit(bars()); }, 3000);
    return true;
  }

  // 自检：把"看着能点、但没被 no-drag 盖住"的元素报出来。
  // 拖动区会吃掉鼠标事件 —— 漏标一个就是"这个按钮点了没反应"，而且极难查（不像报错会留痕迹）。
  // 判据用 cursor:pointer：Chromium 给可点元素算出来的就是这个值。
  function audit(bs) {
    var uncovered = [];
    var covered = 0;
    for (var i = 0; i < bs.length; i++) {
      var all = bs[i].querySelectorAll('*');
      for (var j = 0; j < all.length; j++) {
        var e = all[j];
        if (e.id === '__shell_topgrip') { continue; }
        if (e.matches(HIT) || e.closest(EXTRA)) { covered++; continue; }
        var cur = '';
        try { cur = getComputedStyle(e).cursor; } catch (err) { }
        if (cur === 'pointer') {
          uncovered.push(bs[i].id + ' > ' + e.tagName.toLowerCase()
                         + (e.id ? '#' + e.id : '')
                         + (e.className ? '.' + String(e.className).split(' ')[0] : ''));
        }
      }
    }
    report({
      type: 'page_log',
      step: '[NC] app-region 已注入 | 拖动条=' + bs.map(function (b) { return b.id; }).join('+')
            + ' | 已护住可点元素=' + covered
            + ' | 光标热区漏标=' + uncovered.length
            + (uncovered.length ? ' → ' + uncovered.slice(0, 10).join(' ; ') : '')
    });
  }

  if (apply()) { return; }
  // #top 是页面脚本后来才建的，DOMContentLoaded 那一刻可能还不存在 —— 轮询兜住
  var tries = 0;
  var timer = setInterval(function () {
    if (apply() || ++tries > 40) { clearInterval(timer); }
  }, 250);
  document.addEventListener('DOMContentLoaded', function () { apply(); });
})();
""";

    /// <summary>
    /// 随文档注入的**主题脚本**：在页面脚本之前把 data-theme 设好。
    ///
    /// 为什么不能只靠宿主发消息：页面是"先渲染、后收消息"的。浅色模式下若第一帧按
    /// 默认（深色）配色画，就是**浅底配浅字**的一瞬，看着像闪一下白。
    /// 所以值走 localStorage（宿主在切主题时写进去），这里在 document-created 那一刻读出来。
    ///
    /// 🔴 必须**两种边框模式都注册**：主题跟标题栏形态无关，漏一端就会出现
    ///    "frameless 下浅色正常、原生标题栏下闪一下"这种一半好一半坏的现象。
    /// </summary>
    private const string ThemeInjectJs = """
(function () {
  if (window.__dshTheme) { return; }
  window.__dshTheme = true;

  function apply() {
    // 🔴 document-created 那一刻根节点可能还没建（同 NativeChromeInjectJs 里那条注释），
    //    取不到就返回 false 交给下面的轮询，别在这里抛 —— 一抛整段就死了，而且宿主日志
    //    还写着"已注册"，现象是"注入明明成功、页面却没变"。
    var root = document.documentElement;
    if (!root) { return false; }
    var t = 'dark';
    try {
      var v = localStorage.getItem('dsh.appearance');
      if (v === 'light' || v === 'dark') { t = v; }
    } catch (e) { /* 隐私模式之类读不到，按默认深色 */ }
    root.setAttribute('data-theme', t);
    var m = null;
    try { m = localStorage.getItem('dsh.material'); } catch (e) {}
    if (m) { root.setAttribute('data-material', m); }
    return true;
  }

  if (apply()) { return; }
  var tries = 0;
  var timer = setInterval(function () { if (apply() || ++tries > 20) { clearInterval(timer); } }, 10);
})();
""";

    /// <summary>
    /// 原生标题栏模式随文档注入的脚本：把页面自己画的那条标题栏**整条收掉**。
    ///
    /// 为什么必须收：Windows 已经把「图标 + ADOFAI Studio + — ▢ ✕」画在标题栏里了，
    /// 页面顶上再来一条一模一样的东西，窗口就顶着**两条**标题栏。
    ///
    /// 🔴 只作用于**壳内**：这段是随文档注入的，浏览器 / headless 探针打开同一个页面时看不到，
    ///    所以三套页面在浏览器里的样子一点没变（回归探针的断言也就还成立）。
    ///    改页面源码是另一回事 —— 那会把浏览器里的样子一起改掉，不能干。
    /// </summary>
    private const string NativeChromeInjectJs = """
(function () {
  if (window.__shellNativeChrome) { return; }
  window.__shellNativeChrome = true;

  var STYLE_ID = '__shell_native_chrome';

  // 上报走 chrome.webview，与 NcInjectJs 里那份同款。
  // 🔴 必须在**本闭包内自己定义**：NcInjectJs 的 report 在它自己的闭包里，这里调不到。
  //    本轮真踩：直接调了那个名字，audit 抛 ReferenceError —— 而它紧跟在 appendChild 后面，
  //    于是"样式插没插上"和"自检有没有跑"两件事搅在一起，看不出到底哪步断了。
  function report(obj) {
    try { window.chrome.webview.postMessage(JSON.stringify(obj)); } catch (e) { }
  }

  function apply() {
    if (document.getElementById(STYLE_ID)) { return true; }

    // 🔴 端口：脚本是在 document-created 那一刻跑的，此时 documentElement / head 可能**都还是 null**，
    //    appendChild 当场抛 TypeError，整个脚本就此死掉（连下面的轮询都没跑到）。
    //    表现极具误导性：宿主日志写着"✓ 已注册原生标题栏注入脚本"，页面上却一点变化都没有。
    //    所以：拿不到根节点就返回 false，交给轮询重试。
    var root = document.head || document.documentElement;
    if (!root) { return false; }

    var css = [
      // ① 自绘的窗口按钮有**两套**写法，都得收：
      //    导入页是页面自带的 .win-btns；工作台 / studio 是桥注入的 #win-ctl。
      //    漏一套就会和系统那三个按钮并排成两组。
      '.win-btns, #win-ctl { display: none !important; }',

      // ② 导入页那条自绘标题栏整条收掉。
      //    用 display 而不是 visibility：那条栏里的东西（含「?」）现在**一个都不留**，
      //    整条收掉最干净（历史上前一版是把「?」搬出去、再靠 visibility 收栏尾，
      //    现在不需要了 —— 见 ③）。
      '#titlebar { display: none !important; }',

      // ③ （2026-09-21 起这里是**空的**）原先是「?」的落脚样式。
      //    🔴 主人明确说"不要这个东西了"，所以撤掉钉在右下角的那份 CSS。它本来就长在
      //       那条自绘标题栏上，②把栏收掉之后它自然跟着不见，**不需要单独再钉一次**。
      //       （原先是为了"功能不能消失"才把它搬出来钉着；侧栏「关于」页仍在，功能没丢。）
      //       历史坑留此备查：如果哪天真要把它固定在窗口角上，**不能只写 position:fixed**
      //       —— 那条栏带 backdrop-filter，会创建包含块，fixed 会相对"已被压成 0 高的栏"
      //       定位，人被顶到视口外（实测 rect.top=-40）；必须先把它搬出那条栏。

      // ④ 内容区补回被收掉的那 42px。导入页 #body 写死 height:calc(100% - 42px)，不补就空一条。
      '#body { height: 100% !important; }',

      // ⑤ 上一版（无边框模式）注入过一条 8px 的顶部抓取条，是给页面补缩放热区用的。
      //    原生标题栏的上边缘由系统处理，这条留着只会挡点击。
      '#__shell_topgrip { display: none !important; }'
    ].join('\n');

    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = css;
    root.appendChild(style);

    audit();
    // 页面的窗口按钮可能是脚本后来才建的（工作台/studio 的 #win-ctl 由桥注入）——
    // 隔一会儿再点一次名，别把"后来才出现的"漏过去。
    setTimeout(audit, 2500);
    return true;
  }

  // ★ 2026-09-21 hoistAbout() 已删：那条「?」跟着自绘标题栏一起收掉了，不再搬到窗口角上。
  //   （原先它必须**搬出去**才能钉 —— 留在栏里会跟栏的 backdrop-filter 包含块抢 fixed 基准，
  //    实测人被顶到 y=-40。现在既然不钉了，这步搬家也一并撤掉。）

  // 自检：把"收掉了什么"报进宿主日志。
  // 这一步不是洁癖 —— 少了它，一旦某套页面的选择器变了，表现是"顶上莫名多出一条"，
  // 而看日志只会看到一片正常。数字对不上就能立刻定位。
  // 🔴 判据要**读实测高度**，不能只看"元素在不在"：元素在、高度没归零，就是没生效。
  // 🔴 整段包在 try 里：自检自己出错绝不能反过来把功能搞死（本轮踩过）。
  function audit() {
    try {
      var tb = document.getElementById('titlebar');
      // 🔴 判据读**实测显示状态**，不是"元素还在不在"：元素在、display 没被压掉 = 没生效。
      //    只判存在性的话，"注入没插上"和"插上了但被页面样式压回去"看起来一模一样。
      var tbGone = !tb || getComputedStyle(tb).display === 'none';
      // 「?」现在**不该出现在视口里**（它长在自绘标题栏上，那条栏整体收掉了）。
      // 判据仍读实测宽度：只要还有盒子尺寸，就说明"又被谁露出来了"——日志要能一眼看出回归。
      var ab = document.getElementById('ibAbout');
      var abShown = !!ab && ab.getBoundingClientRect().width > 0;
      report({
        type: 'page_log',
        step: '[NATIVE] 原生标题栏模式 | 自绘标题栏='
              + (tb ? (tbGone ? '已收掉' : '⚠ 没生效，还显示着') : '此页无')
              + ' | 自绘窗口按钮已藏=' + document.querySelectorAll('.win-btns button, #win-ctl button').length + ' 个'
              + ' | 「?」入口=' + (!ab ? '无此页' : (abShown ? '⚠ 又露在视口里了' : '已随标题栏收掉'))
      });
    } catch (e) {
      report({ type: 'page_log', step: '[NATIVE] 自检自己报错了（隐藏效果不受影响）: ' + e });
    }
  }

  if (apply()) { return; }
  // 文档刚建、根节点还没出来 —— 轮询等它。次数给足：慢盘上首屏能拖到几秒。
  var tries = 0;
  var timer = setInterval(function () {
    if (apply() || ++tries > 60) { clearInterval(timer); }
  }, 200);
  document.addEventListener('DOMContentLoaded', function () { apply(); });
})();
""";

    private static bool EnvFlag(string key, bool fallback)
    {
        var raw = Environment.GetEnvironmentVariable(key);
        if (string.IsNullOrWhiteSpace(raw)) return fallback;
        return !(raw == "0"
                 || raw.Equals("false", StringComparison.OrdinalIgnoreCase)
                 || raw.Equals("no", StringComparison.OrdinalIgnoreCase)
                 || raw.Equals("off", StringComparison.OrdinalIgnoreCase));
    }

    private async Task SetupChromeScriptsAsync()
    {
        if (_core is null) return;

        // ★ 主题脚本先注册：它跟"标题栏由谁画"无关，两种模式都要。
        try
        {
            await _core.AddScriptToExecuteOnDocumentCreatedAsync(ThemeInjectJs);
            Log.Write($"✓ 已注册随文档注入的主题脚本（首帧 data-theme={_appearance.Name}）");
        }
        catch (Exception ex)
        {
            Log.Write("注册主题注入脚本失败: " + ex.Message);
        }

        if (NativeChrome)
        {
            // 原生标题栏模式：那片像素归系统管，压根不归 WebView2 —— 所以 app-region 一个都不用注入。
            // 要做的反过来：把页面上原本那条自绘标题栏藏掉（不然窗口顶着两条标题栏）。
            try
            {
                await _core.AddScriptToExecuteOnDocumentCreatedAsync(NativeChromeInjectJs);
                Log.Write("✓ 已注册原生标题栏注入脚本（页面自绘标题栏整条隐藏，窗口按钮交给系统那份）");
            }
            catch (Exception ex)
            {
                Log.Write("注册原生标题栏注入脚本失败: " + ex.Message);
            }
            return;
        }

        if (!_ncRegion)
        {
            Log.Write("非客户区支持：按 ADOFAI_SHELL_NC 关闭 —— 标题栏仍由页面自报拖动（老行为）");
            return;
        }

        try
        {
            _core.Settings.IsNonClientRegionSupportEnabled = true;
            Log.Write("✓ 已开启 WebView2 非客户区支持：页面的 app-region 将变成真·系统标题栏" +
                      "（拖动 / 右键系统菜单 / 双击最大化 由系统接管）");
        }
        catch (Exception ex)
        {
            Log.Write("⚠ 开启非客户区支持失败（WebView2 运行时可能过旧，该特性需 ≥1.0.2420.47）: " + ex.Message);
            return;
        }

        try
        {
            await _core.AddScriptToExecuteOnDocumentCreatedAsync(NcInjectJs);
            Log.Write("✓ 已注册随文档注入的 app-region 样式脚本");
        }
        catch (Exception ex)
        {
            Log.Write("注册注入脚本失败: " + ex.Message);
        }
    }

    /// <summary>窗口状态变化 → 广播给页面（唯一出口，任何途径改变状态都会走到这里）。</summary>
    private void PostWinState(bool force = false)
    {
        var zoomed = WindowState == WindowState.Maximized;
        if (!force && _lastZoomed == zoomed) return;
        _lastZoomed = zoomed;
        Log.Write($"窗口状态广播 zoomed={zoomed}（页面按钮字形跟着走）");
        PostToWeb(new
        {
            type = "winstate",
            zoomed,
            minimized = WindowState == WindowState.Minimized,
            canResize = ResizeMode != ResizeMode.NoResize,
        });
    }

    private void ShowSystemMenu()
    {
        try
        {
            var hwnd = _hwnd != IntPtr.Zero ? _hwnd : new WindowInteropHelper(this).Handle;
            var canMax = ResizeMode is ResizeMode.CanResize or ResizeMode.CanResizeWithGrip;
            var ok = WindowShell.ShowSystemMenu(hwnd, WindowState == WindowState.Maximized, canMax, canMax,
                                                out var fgOk);
            Log.Write(ok ? "已弹出系统原生菜单" + (fgOk ? "" : "（⚠ 没抢到前台 —— 菜单可能点了没反应，"
                                                        + "请先点一下窗口再试）")
                         : "取系统菜单失败（GetSystemMenu 返回空）");
        }
        catch (Exception ex)
        {
            Log.Write("系统菜单失败: " + ex.Message);
        }
    }

    /// <summary>
    /// 尺寸变化后**延迟**再量最大化按钮：立刻量会拿到重排之前的旧矩形。
    /// 连着来多次尺寸变化（拖动缩放）时只认最后一次，别排队打转。
    /// </summary>
    private async Task ScheduleMaxButtonRectQueryAsync()
    {
        var token = ++_rectQueryToken;
        await Task.Delay(400);
        if (token != _rectQueryToken) return;
        await QueryMaxButtonRectAsync();
    }

    /// <summary>
    /// 向页面问「最大化按钮在客户区的哪一块」，换算成设备像素存起来。
    /// WM_NCHITTEST 给的是屏幕坐标且是设备像素，所以要先有这个矩形才能判断
    /// "鼠标是不是压在最大化按钮上"。
    /// 另外它还有一个用途：**探针（tools/wininteract-probe.py）从日志里读这个矩形**
    /// 才知道该往哪儿戳 —— 所以这行日志是探针的输入，别删。
    /// </summary>
    private async Task QueryMaxButtonRectAsync()
    {
        if (_core is null || !_ncRegion) return;
        try
        {
            var raw = await _core.ExecuteScriptAsync(
                "(function(){var e=document.getElementById('maxBtn')||document.getElementById('win-max');" +
                "if(!e)return '';var r=e.getBoundingClientRect();" +
                "return [r.left,r.top,r.width,r.height].join(',');})()");

            var s = string.IsNullOrWhiteSpace(raw) ? "" : JsonSerializer.Deserialize<string>(raw) ?? "";
            var parts = s.Split(',');
            if (parts.Length != 4) return;

            var x = double.Parse(parts[0], System.Globalization.CultureInfo.InvariantCulture);
            var y = double.Parse(parts[1], System.Globalization.CultureInfo.InvariantCulture);
            var w = double.Parse(parts[2], System.Globalization.CultureInfo.InvariantCulture);
            var h = double.Parse(parts[3], System.Globalization.CultureInfo.InvariantCulture);
            if (w <= 0 || h <= 0) return;

            var dpi = System.Windows.Media.VisualTreeHelper.GetDpi(this).DpiScaleX;
            var rect = new Rect(x * dpi, y * dpi, w * dpi, h * dpi);
            if (rect != _maxBtnRect)
                Log.Write($"最大化按钮矩形（设备像素，客户区坐标）= {rect.Left:F0},{rect.Top:F0} {rect.Width:F0}×{rect.Height:F0} (dpi={dpi})");
            _maxBtnRect = rect;
        }
        catch (Exception ex)
        {
            Log.Write("量最大化按钮位置失败: " + ex.Message);
        }
    }

    /// <summary>
    /// 窗口过程。
    ///
    /// 用意是**在系统问"鼠标底下是哪个部位"时回答"这是最大化按钮"（HTMAXBUTTON）**，
    /// 那是 Win11 弹 Snap Layouts 悬停面板的唯一入口。
    ///
    /// 🔴 但 2026-09-21 拿真鼠标实测下来的结论是：**这条路在当前架构下走不通**。
    ///    证据（tools/wininteract-probe.py —— 该探针会先自证"4 个测试点底下确实是壳本人
    ///    且窗口置顶成功"，证不了就直接报"测不了"拒给结论）：
    ///      · WebView2 建起来之前 —— WM_NCHITTEST 正常到达，落在窗口内的坐标能收到；
    ///      · WebView2 建起来之后 —— 把鼠标分别移到「最大化按钮正中 / 标题栏空白 /
    ///        上边缘 3px / 客户区正文」并抖动，宿主**一条都没收到**，而此时
    ///        IsNonClientRegionSupportEnabled 已开、页面上 app-region 确实生效
    ///        （注入自检：拖动条=titlebar/top、可点元素护住 4/9 个、漏标=0）。
    ///    也就是说 WebView2 是**自己内部**处理 caption 命中测试的，不转交父窗口；
    ///    能拿到 HTMAXBUTTON 的前提是改用 WebView2 的 composition hosting
    ///    （CoreWebView2CompositionController 自己接管渲染与命中测试），
    ///    那等于换掉整个承载方式，风险远大于收益。
    ///
    /// 所以这段保留为**探针 + 未来兼容**：命中计数器会写在日志里，哪天 WebView2 改成
    /// 转交父窗口了，它自动就生效，不用再改代码。真实的 Snap 入口用系统自带的：
    /// Win+Z（当前窗口的布局面板）与「拖到屏幕边缘」（真·系统拖动，本来就通）。
    /// </summary>
    private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        switch (msg)
        {
            case WindowShell.WmNcHitTest:
                {
                    var sx = unchecked((short)(long)lParam);
                    var sy = unchecked((short)((long)lParam >> 16));

                    // 只统计**落在这个窗口上**的命中测试。
                    // 注意别把限流浪费在窗口外面：WebView2 还没建起来那几百毫秒里，
                    // 系统会拿窗口外的坐标问一堆，头 5 条配额会被它吃光（踩过）。
                    var wr = WindowShell.WindowRect(hwnd);
                    var inside = sx >= wr.Left && sx < wr.Right && sy >= wr.Top && sy < wr.Bottom;
                    if (inside)
                    {
                        _hitTestInWindow++;
                        if (_hitTestInWindow <= 5 || _hitTestInWindow % 200 == 0)
                            Log.Write($"WM_NCHITTEST #{_hitTestInWindow} 落在窗口内 ({sx},{sy})");
                    }

                    if (!_maxBtnRect.IsEmpty)
                    {
                        var origin = WindowShell.ClientOrigin(hwnd);
                        var px = sx - origin.X;
                        var py = sy - origin.Y;
                        if (px >= _maxBtnRect.Left && px < _maxBtnRect.Right &&
                            py >= _maxBtnRect.Top && py < _maxBtnRect.Bottom)
                        {
                            if (_hitTestMaxHits < 5)
                            {
                                Log.Write($"★ 命中最大化按钮 ({px},{py}) → 回 HTMAXBUTTON，" +
                                          "Win11 Snap Layouts 悬停应由系统接管");
                                _hitTestMaxHits++;
                            }
                            handled = true;
                            return (IntPtr)WindowShell.HtMaxButton;
                        }
                    }
                    break;
                }

            case WindowShell.WmSize:
                // 窗口尺寸一变，页面上那个按钮的位置也跟着变，重新量一次。
                // 🔴 必须**等页面重排完**再量：WM_SIZE 是紧接着尺寸变化就来的，
                //    此刻 DOM 还是旧布局，量回来的是旧矩形（实测踩到：最大化之后
                //    宿主日志里的矩形一直没更新，导致拿旧坐标去点按钮点空）。
                _ = ScheduleMaxButtonRectQueryAsync();
                break;
        }
        return IntPtr.Zero;
    }

    // ---------------------------------------------------------------- 宿主 → 页面

    /// <summary>
    /// 宿主 → 页面发一条消息。
    ///
    /// 🔴 两个硬约束，都是踩出来的：
    /// ① **必须发字符串**（PostWebMessageAsString），不能发对象（PostWebMessageAsJson）。
    ///    导入页的接收端写死了 `d = JSON.parse(e.data)`（index.html:1185），
    ///    拿到对象会抛异常并被 `catch(_){return;}` 静默吞掉 —— 症状是宿主明明处理了、
    ///    日志也打了，界面却停在"载入中…/体检中…"，全链路一条不回。老壳用的是
    ///    PostWebMessageAsString(json.dumps(...))，所以这里是格式契约，不能自由发挥。
    ///    （工作台那份桥两种都认，但导入页只认字符串 ⇒ 只能统一发字符串。）
    /// ② **必须在 UI 线程调用**。老壳为此专门做了队列 + PostMessageW 唤醒 UI 线程，
    ///    注释里写明「工作线程直接调会被静默吞掉，症状是 GPU 早跑完了界面还停在处理中」。
    ///    C# 侧等价做法：不在 UI 线程就 Dispatcher 切回去。
    /// </summary>
    private void PostToWeb(object payload)
    {
        try
        {
            var json = JsonSerializer.Serialize(payload);
            if (!Dispatcher.CheckAccess())
            {
                Dispatcher.Invoke(() => PostToWebRaw(json));
                return;
            }
            PostToWebRaw(json);
        }
        catch (Exception ex)
        {
            Log.Write("PostToWeb 失败: " + ex.Message);
        }
    }

    private void PostToWebRaw(string json)
    {
        try
        {
            _core?.PostWebMessageAsString(json);
        }
        catch (Exception ex)
        {
            Log.Write("PostToWeb(投递) 失败: " + ex.Message);
        }
    }

    /// <summary>
    /// 把 sidecar 的 /api/schema 转成页面要的 schema 消息（对齐老壳 do_get_schema）。
    /// 🔴 必须先等 sidecar 就绪：页面是"秒开"的，第 40 毫秒就发 get_schema，
    ///    而 Python sidecar 冷启动要 3~5 秒 —— 直接去取必然扑空（实测踩到）。
    /// </summary>
    private async void PostSchema()
    {
        try
        {
            var ready = await Task.Run(() => Sidecar.WaitHealthy(60));
            if (!ready)
            {
                Log.Write("获取参数失败：sidecar 60 秒内未就绪");
                PostToWeb(new { type = "schema", ok = false, msg = "引擎还在启动（超 60 秒未就绪），稍后刷新页面重试" });
                return;
            }
            var txt = await _http.GetStringAsync($"http://127.0.0.1:{Paths.SidecarPort}/api/schema");
            using var doc = JsonDocument.Parse(txt);
            PostToWeb(new { type = "schema", ok = true, schema = doc.RootElement.Clone() });
            Log.Write("✓ 参数 schema 已推给页面");
        }
        catch (Exception ex)
        {
            Log.Write("获取参数失败: " + ex.Message);
            PostToWeb(new { type = "schema", ok = false, msg = "获取参数失败：" + ex.Message });
        }
    }

    private void PostAppInfo()
    {
        var d = new Dictionary<string, object?>
        {
            ["app"] = "ADOFAI Studio",
            ["shell"] = "csharp-webview2",
            ["studio_dir"] = Paths.StudioRoot,
            ["output_dir"] = Paths.OutputDir,
            ["audio_sep_dir"] = Path.Combine(Paths.StudioRoot, "audio-sep"),
            ["chartgen_dir"] = Paths.ChartgenDir,
            ["gateway"] = $"http://127.0.0.1:{_gateway.Port}",
            ["sidecar"] = "127.0.0.1:" + Paths.SidecarPort,
            ["cwd"] = Directory.GetCurrentDirectory(),
            ["py_ver"] = "",                          // 新壳不是 Python 进程了，如实留空
            ["material"] = _material.Name,
            ["materials"] = Materials.ToWire(),
            ["appearance"] = _appearance.Name,
            ["appearances"] = Appearances.ToWire(),
            ["explain_candidates"] = HostApi.ExplainCandidates,
            ["webview_cache"] = Paths.WebViewCache,
            ["log"] = Log.File,
        };
        PostToWeb(new { type = "appinfo", data = d });
    }

    /// <summary>
    /// 宿主自走验收（ADOFAI_SHELL_SELFTEST=1 时启动）：
    /// 让壳自己把侧边栏每一页点一遍，每页读完 DOM 把**真实数据**记进日志。
    /// 这是"没有真人在场时，怎么证明宿主每条消息分支都答对了"的办法 ——
    /// 等价于主人手动点一遍，只是结果落在日志里。
    /// 工作台放最后：点它会真导航到另一张页面，当前页的 JS 上下文会销毁。
    /// </summary>
    private async Task RunHostWalkAsync()
    {
        var navs = new[] { "history", "separate", "train", "settings", "about", "generate" };
        await Task.Delay(1500);
        foreach (var p in navs)
        {
            await Eval($"document.querySelector('.nav[data-page=\"{p}\"]').click();");
            await Task.Delay(2200);
            Log.Write($"WALK[{p}] {await Eval(ProbeJs)}");
        }
        // ---- 最后一站：真正进工作台 ----
        // 注意：「点侧边栏」只是打开这一页；要真进去还得选一条 MIDI 再点「前往工作台」
        // （第一版只点了侧边栏，结果量到的还是导入页 —— 探针假通过，别信）
        await Eval("document.querySelector('.nav[data-page=\"workbench\"]').click();");
        await Task.Delay(2500);
        Log.Write("WALK[wb入口] " + await Eval(
            "JSON.stringify({rows: document.querySelectorAll('#midiList .item').length, " +
            "btnDisabled: document.getElementById('openWbBtn').disabled, " +
            "emptyShown: getComputedStyle(document.getElementById('wbEmptyCard')).display !== 'none'})"));

        await Eval("document.querySelector('#midiList .item').click();");
        await Task.Delay(600);
        await Eval("document.getElementById('openWbBtn').click();");
        await Task.Delay(11000);
        // 工作台是另一张页面了：读它的真实状态（是否加载了谱面、桥在不在）
        Log.Write("WALK[workbench] " + await Eval("""
(function(){
  try{
    var s = (window.__dsh && window.__dsh.status) ? window.__dsh.status() : null;
    return JSON.stringify({
      url: location.pathname,
      hasDsh: !!window.dsh,
      hasDshPrivate: !!window.__dsh,
      status: s,
      title: document.title,
      text: (document.body.innerText || '').replace(/\s+/g,' ').slice(0, 200)
    });
  }catch(e){ return 'PROBE_ERR:' + e; }
})()
"""));
    }

    /// <summary>每页共用的 DOM 取数脚本：列表条数 / 关键文案 / 空态是否亮着。</summary>
    private const string ProbeJs = """
(function(){
  function n(sel){ return document.querySelectorAll(sel).length; }
  function txt(id, len){
    var e = document.getElementById(id);
    if(!e) return '(无此元素)';
    return e.innerText.replace(/\s+/g, ' ').slice(0, len);
  }
  var we = document.getElementById('wbEmpty');
  return JSON.stringify({
    page: (document.querySelector('.page.active') || {}).id || '(无)',
    hist: n('#histList .item'),
    midi: n('#midiList .item'),
    /* 2026-09-22：分离试听改成**折叠组**（默认只露歌名一行）⇒
       "看得见几条"要看 .stem-group（组头）；.stem-row 仍在 DOM 里但被
       .stem-body 收起（`display:none`），只报它会把"收着的"也算成"露着的"。*/
    stemGroups: n('#stemList .stem-group'),
    stemRows: n('#stemList .stem-row'),
    kv: n('.kv'),
    /* 2026-09-22：材质 4 张卡 → 自绘下拉框 ShellUI.Dropdown（#matGrid 已删）。
       菜单节点在组件创建时就挂进 body（收起时 hidden），所以这里数得到选项。*/
    matOptions: n('#matSelect-menu .dd-opt'),
    appearOptions: n('#appearSelect-menu .dd-opt'),
    explainMissing: (function(){
      var e = document.getElementById('explainMissing');
      return !!(e && getComputedStyle(e).display !== 'none');
    })(),
    explainMissingText: txt('explainMissing', 90),
    wbEmptyShown: !!(we && getComputedStyle(we).display !== 'none'),
    wbEmptyText: txt('wbEmptyText', 80),
    clean: txt('cleanStatus', 70),
    about: txt('aboutCard', 200),
    train: txt('trainCard', 220)
  });
})()
""";

    private async Task<string> Eval(string js)
    {
        try
        {
            var raw = await _core!.ExecuteScriptAsync(js);
            if (raw.Length > 1 && raw[0] == '"')
            {
                try { return JsonSerializer.Deserialize<string>(raw) ?? raw; }
                catch { return raw; }
            }
            return raw;
        }
        catch (Exception ex)
        {
            return "(执行失败: " + ex.Message + ")";
        }
    }

    /// <summary>
    /// 单向收了消息不代表双向通 —— 主动往页面里塞一段脚本，要求它回一条消息。
    /// 对应老壳 gui_main.py 的 _start_selftest()。
    /// </summary>
    private async Task RunSelfTestAsync()
    {
        await Task.Delay(700);
        try
        {
            await _core!.ExecuteScriptAsync(
                "window.chrome.webview.postMessage(JSON.stringify({" +
                "type:'selftest_pong'," +
                "hasChromeWebview: !!(window.chrome && window.chrome.webview)," +
                "hasDsh: typeof window.dsh !== 'undefined'," +
                "hasDshPrivate: typeof window.__dsh !== 'undefined'," +
                "activePage: (document.querySelector('.page.active') || {}).id || '(无)'," +
                "navCount: document.querySelectorAll('#sidebar .nav').length," +
                "histRows: document.querySelectorAll('#histList .item').length," +
                "url: location.href}));");
            Log.Write("自检：已要求页面回击");
        }
        catch (Exception ex)
        {
            Log.Write("自检失败: " + ex.Message);
        }
    }
}
