namespace AdofaiStudio;

/// <summary>
/// 一档界面明暗（深色 / 浅色）。对齐老壳 gui_main.py 里那条写死的 <c>set_dark(hwnd)</c> ——
/// 老壳只有深色，这里是新加的：主人 2026-09-22 要求「点到云母和实色的时候跳出一个深色/浅色的选项」。
///
/// <paramref name="BaseHex"/> 是这一档的**基底色**：
///   - 实色档的客户区底色（WebView DefaultBackgroundColor）
///   - 实色档的标题栏颜色（DWM caption color，见 Dwm.SetCaptionColor 的注释）
///   - 页面浅色主题的配色也以它为准（gui/index.html 里的 --bg-base）
/// 云母/亚克力不用它：材质的底色由系统画，我们只负责把标题栏还给材质。
/// </summary>
public sealed record AppearanceSpec(string Name, bool Dark, string Label, string BaseHex);

public static class Appearances
{
    public const string Default = "dark";

    /// <summary>顺序 = 前端小胶囊的顺序，别随意调（浅色在后，跟 Win11 设置里"深色/浅色"一致）。</summary>
    public static readonly IReadOnlyList<AppearanceSpec> All =
    [
        new("dark",  true,  "深色", "#191919"),
        new("light", false, "浅色", "#F3F3F3"),
    ];

    public static AppearanceSpec Resolve(string? name)
    {
        foreach (var a in All)
            if (string.Equals(a.Name, name, StringComparison.Ordinal))
                return a;

        // 兜底按名字找默认档，不写死下标（同 Materials.Resolve 的理由：增删档位时下标会静默指错）。
        foreach (var a in All)
            if (a.Name == Default)
                return a;
        return All[0];
    }

    /// <summary>发给前端的档位清单（前端只维护"要不要显示选择器"，不再自己编名字）。</summary>
    public static object[] ToWire() =>
        All.Select(a => (object)new { name = a.Name, label = a.Label, dark = a.Dark, base_hex = a.BaseHex })
           .ToArray();
}
