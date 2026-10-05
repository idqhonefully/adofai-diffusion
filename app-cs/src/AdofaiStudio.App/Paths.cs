using System.IO;

namespace AdofaiStudio;

/// <summary>
/// 全局路径与端口 —— 严格对齐老壳 gui_main.py / backend.py 的常量，
/// 目的：新旧两个壳能共用同一份数据目录（ui-prefs.json、ui-layout.json、webview_cache），
/// 切换壳的时候用户设置不丢。
/// </summary>
public static class Paths
{
    public static string StudioRoot { get; } = ResolveStudioRoot();

    /// <summary>我们自己的程序目录（前端页面与桥脚本都在这）。</summary>
    public static string GuiDir => Path.Combine(StudioRoot, "gui");

    /// <summary>数据目录。老壳里 DATA_DIR == OUTPUT_DIR。</summary>
    public static string OutputDir => Path.Combine(StudioRoot, "output");

    /// <summary>WebView2 的缓存 —— 默认会落在 C:\Users\...\AppData，必须改道到 D 盘。</summary>
    public static string WebViewCache => Path.Combine(OutputDir, "webview_cache");

    public static string UiLayoutFile => Path.Combine(OutputDir, "ui-layout.json");
    public static string UiPrefsFile => Path.Combine(OutputDir, "ui-prefs.json");

    /// <summary>Adofai-Chart-Generator引擎（Python，作为后端子进程跑）。</summary>
    public static string ChartgenDir => Path.Combine(StudioRoot, "chartgen");

    /// <summary>音频分离引擎目录（BS-RoFormer-SW 及其依赖都在这）。</summary>
    public static string AudioSepDir => Path.Combine(StudioRoot, "audio-sep");

    /// <summary>
    /// 🔴 全工程唯一的 Python 解释器（2026-10-04 运行时融合后定死在这）：
    ///    chartgen 与 audio-sep 共用 audio-sep/runtime 这一份；
    ///    原先 chartgen/runtime 那份（3.14）已删除，其依赖是这份的严格子集。
    ///    任何地方都别再自己拼 python 路径 —— Sidecar / HostApi / MainWindow 一律引这里，
    ///    否则删改运行时就会漏改（曾因 Sidecar 单独硬编码 chartgen/runtime 导致 sidecar 起不来）。
    /// </summary>
    public static string PythonExe => Path.Combine(AudioSepDir, "runtime", "python.exe");

    /// <summary>Adofai-Chart-Generator引擎 sidecar 端口（老壳 backend.PORT）。</summary>
    public static int SidecarPort => EnvInt("CHARTGEN_PORT", 8765);

    /// <summary>同源网关端口（老壳 backend.GATEWAY_PORT）。</summary>
    public static int GatewayPort => EnvInt("STUDIO_GATEWAY_PORT", 8766);

    public static string GatewayBase => $"http://127.0.0.1:{GatewayPort}";

    /// <summary>前端页面地址：导入页 / Adofai-Chart-Generator。</summary>
    public static string ImportUrl => GatewayBase + "/index.html";
    public static string WorkbenchUrl => GatewayBase + "/workbench/index.html";

    private static int EnvInt(string key, int fallback)
    {
        var raw = Environment.GetEnvironmentVariable(key);
        return int.TryParse(raw, out var v) && v > 0 ? v : fallback;
    }

    private static string ResolveStudioRoot()
    {
        var fromEnv = Environment.GetEnvironmentVariable("ADOFAI_STUDIO_ROOT");
        if (!string.IsNullOrWhiteSpace(fromEnv) && Directory.Exists(fromEnv))
            return Path.GetFullPath(fromEnv);

        var baseDir = new DirectoryInfo(AppContext.BaseDirectory);

        // dev 场景：exe 在 app-cs\out\...\publish\ 内 → 往上找到 app-cs，其父即 studio 根
        var dir = baseDir;
        for (var i = 0; i < 10 && dir is not null; i++)
        {
            if (string.Equals(dir.Name, "app-cs", StringComparison.OrdinalIgnoreCase) &&
                dir.Parent is not null)
                return dir.Parent.FullName;
            dir = dir.Parent;
        }

        // 便携包场景：启动.exe 就在 studio 根，gui/audio-sep/chartgen 是同级的兄弟目录
        // （不依赖任何固定盘符/路径，拷贝到任意目录双击即跑）
        if (Directory.Exists(Path.Combine(baseDir.FullName, "gui")) &&
            Directory.Exists(Path.Combine(baseDir.FullName, "audio-sep")) &&
            Directory.Exists(Path.Combine(baseDir.FullName, "chartgen")))
            return baseDir.FullName;

        // 兜底：极端情况才走到（正常分发走 exe 同级 / 环境变量 / dev 向上回溯）。
        // 不再写死任何固定盘符路径 —— 直接用程序所在目录作为最后手段。
        return baseDir.FullName;
    }
}
