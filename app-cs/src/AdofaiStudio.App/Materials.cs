using System.Linq;
using AdofaiStudio.Native;

namespace AdofaiStudio;

/// <summary>一档界面材质。对齐老壳 gui_main.py 的 MATERIALS 字典，顺序也必须一致
/// （前端按这个顺序渲染卡片，顺序变了 UI 就换了位置）。
///
/// ★ 2026-09-22 起 <c>Kinds</c> 全部是"系统材质"，除了「实色」。
///   明暗（深色/浅色）**不再是某一档材质的属性** —— 它是全局外观（见 Appearances.cs），
///   因为浅色并不只属于云母/实色（亚克力、标签页也都有浅色版）。
///   原先那个 <c>DarkLight</c> 字段与 per-material 的 <c>appearances</c> 已随之删除，
///   页面上改在材质卡下面**另起一行**做全局切换。
/// </summary>
public sealed record MaterialSpec(string Name, int Kind, bool Opaque, string Label);

public static class Materials
{
    public const string Default = "acrylic";

    /// <summary>
    /// 档位清单。顺序 = 前端下拉里的顺序，别随意调。
    /// 🔴 2026-09-21 主人裁定：**删掉「透明」档**（不施任何材质那一档）。
    ///    它跟「纯色」是同一个 DWM kind（都不施材质），区别只在 WebView 底透不透 ——
    ///    实际观感就是"和纯色几乎一样、却更脏"，留着只会让人多点一次。
    ///    老值（ui-prefs.json 里存的 "transparency"）由 Resolve 兜回默认档，不用清配置。
    /// ★ 2026-09-22 主人要求：**加「标签页」档**（Win11 22H2 的 Tabbed 材质，
    ///   资源管理器带标签页那种、比云母更沉一点）。
    ///   插在「云母」后面，把三档系统材质排在一起，「纯色」留在最后 ——
    ///   它严格说不是材质，是"不施材质 + 刷一整片颜色"。老壳 MATERIALS 同步加。
    ///   DWM 取值说明见 Dwm.BackdropTabbed 的注释（常量叫 Tabbed，值是 4 而不是 3）。
    /// ★ 2026-09-22 主人又要求：最后一档的名字 由「实色」改成「纯色」。
    /// ★ 2026-09-22 主人再要求（第二十三轮）：**四档 label 全部改成英文**
    ///   （Acrylic / Mica / Tabbed / Solid）—— 他说中文档位名"太土"。
    ///   🔴 **只动 label，绝不动 name**：name 是 ui-prefs.json 里落盘的配置键、
    ///      也是前端认档位的键（`data-v`、`setmaterial` 消息都用它），改了就读不出旧配置。
    ///   ⚠ label 一改，`_whichbuild.py` 里靠中文 label 认版本的标记就失效了 ——
    ///      已同步换成 `Tabbed` / `Solid` 这两个英文串，并把旧中文名移进负标记。
    /// </summary>
    public static readonly IReadOnlyList<MaterialSpec> All =
    [
        new("acrylic", Dwm.BackdropAcrylic, false, "Acrylic"),
        new("mica",    Dwm.BackdropMica,    false, "Mica"),
        new("tabbed",  Dwm.BackdropTabbed,  false, "Tabbed"),
        new("solid",   Dwm.BackdropNone,    true,  "Solid"),
    ];

    /// <summary>「实色」档的便捷引用（Win10 降级时用，见 MainWindow.EffectiveMaterial）。</summary>
    public static MaterialSpec Solid { get; } = All.First(m => m.Name == "solid");

    /// <summary>名字不认识就回退默认档（老壳同款行为）。</summary>
    public static MaterialSpec Resolve(string? name)
    {
        foreach (var m in All)
            if (string.Equals(m.Name, name, StringComparison.Ordinal))
                return m;

        // 🔴 兜底不能写死下标：删了「透明」这档之后 All[0] 已经换人，
        //    写死的下标会在下次增删档位时静默指错（表现是"选了亚克力却给了别的"）。
        foreach (var m in All)
            if (m.Name == Default)
                return m;
        return All[0];
    }

    /// <summary>发给前端 appinfo 的材质清单。
    /// ⚠ 不再带 <c>appearances</c>：明暗是全局的，不由材质档决定（见类注释）。</summary>
    public static object[] ToWire() =>
        All.Select(m => (object)new
        {
            name = m.Name,
            label = m.Label,
            kind = m.Kind,
            opaque = m.Opaque,
        }).ToArray();
}
