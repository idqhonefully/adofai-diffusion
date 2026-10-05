using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace AdofaiStudio.Services;

/// <summary>
/// 宿主侧数据源 —— 对齐老壳 gui_main.py 里的那一批 do_* / send_history 函数。
/// 全部是"读磁盘 + 回消息"，没有副作用（除了 cleanwork / mkexplain）。
/// </summary>
public static class HostApi
{
    // ---------- 候选路径（与老壳常量一字不差）----------

    /// <summary>「关于」页读的 explain.md：只认程序目录（不会被"清理临时文件"波及）。</summary>
    public static string[] ExplainCandidates => new[]
    {
        Path.Combine(Paths.GuiDir, "explain.md"),
        Path.Combine(Paths.GuiDir, "studio", "explain.md"),
        Path.Combine(Paths.GuiDir, "docs", "explain.md"),
    };

    /// <summary>
    /// ⚠ 本项目仓库里**当前不存在**训练代码。这里只做「入口 + 环境体检 + 缺件如实提示」，
    ///   绝不代主人启动训练（铁律）。脚本放进来后本页自动点亮。
    /// </summary>
    public static string[] TrainCandidates => new[]
    {
        Path.Combine(Paths.StudioRoot, "audio-sep", "train_onset.py"),
        Path.Combine(Paths.GuiDir, "studio", "train", "train_onset.py"),
        Path.Combine(Paths.StudioRoot, "train", "train_onset.py"),
    };

    public static string[] TrainDataCandidates => new[]
    {
        Path.Combine(Paths.StudioRoot, "dataset", "melody"),
        Path.Combine(Paths.StudioRoot, "train", "dataset"),
        Path.Combine(Paths.StudioRoot, "audio-sep", "dataset"),
    };

    private static string WorkRoot => Path.Combine(Paths.OutputDir, ".work");

    private static readonly string[] MidiAudioExt = { ".wav", ".mp3", ".flac", ".m4a", ".ogg" };

    // ---------- 通用小工具 ----------

    public static string HumanSize(long n) =>
        n >= 1024 * 1024 ? $"{n / 1024.0 / 1024.0:F1} MB" : $"{n / 1024.0:F1} KB";

    /// <summary>output/.work 下最新一次生成的合成 MIDI（*_stems_combined.mid），无则 null。</summary>
    public static string? LatestCombinedMidi()
    {
        try
        {
            if (!Directory.Exists(WorkRoot)) return null;
            var cands = Directory.GetFiles(WorkRoot, "*_stems_combined.mid", SearchOption.AllDirectories);
            if (cands.Length == 0)
                cands = Directory.GetFiles(WorkRoot, "*.mid", SearchOption.AllDirectories);
            if (cands.Length == 0) return null;
            return cands.OrderByDescending(File.GetLastWriteTimeUtc).First();
        }
        catch (Exception ex)
        {
            Log.Write("LatestCombinedMidi 失败: " + ex.Message);
            return null;
        }
    }

    /// <summary>在工作目录里，按歌曲名匹配最近一次生成的 *stems_combined.mid。</summary>
    public static string? FindSourceMidi(string adofaiPath)
    {
        try
        {
            var name = Path.GetFileNameWithoutExtension(adofaiPath).ToLowerInvariant();
            var all = Directory.GetFiles(WorkRoot, "*.mid", SearchOption.AllDirectories)
                .OrderByDescending(File.GetLastWriteTimeUtc);
            foreach (var m in all)
            {
                var b = Path.GetFileName(m).ToLowerInvariant();
                if (b.Contains(name) || name.Contains(b.Replace("_stems_combined.mid", "")))
                    return m;
            }
        }
        catch (Exception ex)
        {
            Log.Write("FindSourceMidi 失败: " + ex.Message);
        }
        return null;
    }

    // ---------- ① 历史记录（已生成的 .adofai）----------

    public static object History()
    {
        var items = new List<object>();
        try
        {
            foreach (var fp in Directory.GetFiles(Paths.OutputDir, "*.adofai", SearchOption.AllDirectories))
            {
                long sz;
                DateTime mt;
                try { sz = new FileInfo(fp).Length; mt = File.GetLastWriteTimeUtc(fp); }
                catch { continue; }
                items.Add(new
                {
                    name = Path.GetFileName(fp),
                    size = HumanSize(sz),
                    path = fp,
                    source_dir = Path.GetDirectoryName(fp) ?? "",
                    mtime = new DateTimeOffset(mt).ToUnixTimeMilliseconds(),
                    source_midi = FindSourceMidi(fp) ?? "",
                });
            }
            items = items.OrderByDescending(o =>
                (long)o.GetType().GetProperty("mtime")!.GetValue(o)!).ToList();
        }
        catch (Exception ex)
        {
            Log.Write("History 失败: " + ex.Message);
        }
        return new { type = "history", items };
    }

    // ---------- ② 工作台入口的历史 MIDI 列表 ----------

    public static object Midis()
    {
        var items = new List<Dictionary<string, object?>>();
        try
        {
            // 先一次性把 output 下的谱面名收齐，再逐条匹配 —— 别每条 MIDI 都递归扫一遍
            var charts = new List<string>();
            try
            {
                foreach (var x in Directory.GetFiles(Paths.OutputDir, "*.adofai", SearchOption.AllDirectories))
                    charts.Add(Path.GetFileName(x));
            }
            catch { /* 忽略 */ }

            var cands = Directory.Exists(WorkRoot)
                ? Directory.GetFiles(WorkRoot, "*_stems_combined.mid", SearchOption.AllDirectories)
                : Array.Empty<string>();
            if (cands.Length == 0 && Directory.Exists(WorkRoot))
                cands = Directory.GetFiles(WorkRoot, "*.mid", SearchOption.AllDirectories);

            foreach (var fp in cands.Distinct())
            {
                var job = Path.GetDirectoryName(fp) ?? "";
                var song = Path.GetFileName(job);

                // 这一次生成用的整曲：流水线第①步落的 job/input.wav。
                // 🔴 注意**比想的深一层**：MIDI 在 .work/<歌曲>_<时间戳>/，job/ 是它的子目录
                //    ⇒ 真实路径是 <job>/job/input.wav（工作台前端探的就是这层）。
                var audio = "";
                foreach (var c in new[]
                         {
                             Path.Combine(job, "job", "input.wav"),
                             Path.Combine(job, "input.wav"),
                         })
                {
                    if (File.Exists(c)) { audio = c; break; }
                }
                if (audio.Length == 0)
                {
                    foreach (var sub in new[] { Path.Combine(job, "job"), job })
                    {
                        if (!Directory.Exists(sub)) continue;
                        foreach (var ext in MidiAudioExt)
                        {
                            var hit = Directory.GetFiles(sub, "*" + ext);
                            if (hit.Length > 0) { audio = hit[0]; break; }
                        }
                        if (audio.Length > 0) break;
                    }
                }

                long sz = 0; DateTime mt = default;
                try { sz = new FileInfo(fp).Length; mt = File.GetLastWriteTimeUtc(fp); } catch { /* 忽略 */ }

                items.Add(new Dictionary<string, object?>
                {
                    ["name"] = Path.GetFileName(fp),
                    ["song"] = song,
                    ["path"] = fp,
                    ["job"] = job,
                    ["size"] = HumanSize(sz),
                    ["bytes"] = sz,
                    ["mtime"] = mt == default ? 0 : new DateTimeOffset(mt).ToUnixTimeMilliseconds(),
                    ["audio"] = audio,
                    ["has_chart"] = charts.Any(c => c.Contains(song, StringComparison.OrdinalIgnoreCase)),
                });
            }
        }
        catch (Exception ex)
        {
            Log.Write("Midis 失败: " + ex.Message);
        }

        items = items.OrderByDescending(o => (long)o["mtime"]!).ToList();
        return new { type = "midis", items, work_dir = WorkRoot, count = items.Count };
    }

    // ---------- ③ 分轨列表（全部 job，按歌曲折叠）----------
    // 遍历 output/.work 下所有含 stems/*.wav 的目录，每首歌一个折叠项，
    // 由前端 renderStems 渲染（点击歌名展开该曲各音轨）。OSN1 路径不落盘分轨，故不出现。

    public static object Stems()
    {
        var jobs = new List<Dictionary<string, object?>>();
        try
        {
            if (Directory.Exists(WorkRoot))
            {
                foreach (var dir in Directory.GetDirectories(WorkRoot))
                {
                    var stemDir = Path.Combine(dir, "stems");
                    if (!Directory.Exists(stemDir)) continue;
                    var wavs = Directory.GetFiles(stemDir, "*.wav").OrderBy(x => x).ToArray();
                    if (wavs.Length == 0) continue;

                    var items = new List<object>();
                    long latest = 0;
                    foreach (var s in wavs)
                    {
                        var mt = new DateTimeOffset(File.GetLastWriteTimeUtc(s)).ToUnixTimeMilliseconds();
                        if (mt > latest) latest = mt;
                        items.Add(new
                        {
                            name = Path.GetFileName(s),
                            path = s,
                            mtime = mt,
                        });
                    }
                    jobs.Add(new Dictionary<string, object?>
                    {
                        ["job"] = dir,
                        ["song"] = Path.GetFileName(dir),
                        ["items"] = items,
                        ["mtime"] = latest,
                    });
                }
            }
        }
        catch (Exception ex)
        {
            Log.Write("Stems 失败: " + ex.Message);
        }
        // 按最近一次分离的 wav 时间倒序：最近分离的歌排最前
        jobs = jobs.OrderByDescending(j => (long)j["mtime"]!).ToList();
        return new { type = "stems", jobs };
    }

    // ---------- ④ explain.md ----------

    public static object Explain()
    {
        const int maxc = 512 * 1024;
        foreach (var p in ExplainCandidates)
        {
            try
            {
                if (!File.Exists(p)) continue;
                var text = ReadCapped(p, maxc);
                Log.Write($"explain: 读取 {p}（{text.Length} 字节）");
                return new
                {
                    type = "explain",
                    ok = true,
                    path = p,
                    text,
                    size = HumanSize(new FileInfo(p).Length),
                    candidates = ExplainCandidates,
                };
            }
            catch (Exception ex)
            {
                Log.Write($"Explain 读取失败({p}): {ex.Message}");
            }
        }
        Log.Write("explain: 未找到 explain.md");
        return new { type = "explain", ok = false, path = "", candidates = ExplainCandidates };
    }

    private static string ReadCapped(string path, int maxBytes)
    {
        using var fs = File.OpenRead(path);
        var buf = new byte[Math.Min(maxBytes, fs.Length)];
        var got = 0;
        while (got < buf.Length)
        {
            var n = fs.Read(buf, got, buf.Length - got);
            if (n <= 0) break;
            got += n;
        }
        return Encoding.UTF8.GetString(buf, 0, got);
    }

    public const string ExplainTemplate = """
# ADOFAI Studio 说明

在这里写你的说明文档，保存成 **UTF-8** 的 `.md`，回到「关于」页就会自动渲染。

## 这个文件能写什么

- 标题（`#`、`##`、`###`）
- **粗体**、*斜体*、`行内代码`
- 列表、> 引用、``` 代码块 ```
- 表格、链接、图片、分隔线（`---`）

## 生成链路

| 步骤 | 做什么 | 工具 |
|---|---|---|
| ① | 转 WAV（44.1k 立体声） | ffmpeg |
| ② | 分离 6 轨 | BS-RoFormer-SW |
| ③ | 逐轨转谱 | MuScriptor |
| ④ | 合并成单文件多轨 MIDI | 自家脚本 |
| ⑤ | 出谱（.adofai） | Adofai-Chart-Generator算法 |

---

写完直接刷新「关于」页即可，不需要重启。
""";

    public static object MkExplain(bool force)
    {
        var p = ExplainCandidates[0];
        try
        {
            if (File.Exists(p) && !force)
                return new { type = "explain_written", ok = false, path = p, msg = "文件已存在，没有覆盖" };
            Directory.CreateDirectory(Path.GetDirectoryName(p)!);
            File.WriteAllText(p, ExplainTemplate, new UTF8Encoding(false));
            Log.Write("mkexplain: 写入 " + p);
            return new { type = "explain_written", ok = true, path = p };
        }
        catch (Exception ex)
        {
            Log.Write("MkExplain 失败: " + ex.Message);
            return new { type = "explain_written", ok = false, path = p, msg = ex.Message };
        }
    }

    // ---------- ⑤ 训练页环境体检（只体检，绝不启动）----------

    public static object TrainInfo()
    {
        var script = TrainCandidates.FirstOrDefault(File.Exists) ?? "";

        var dataDir = "";
        long audioN = 0;
        foreach (var p in TrainDataCandidates)
        {
            if (!Directory.Exists(p)) continue;
            dataDir = p;
            try
            {
                foreach (var f in Directory.EnumerateFiles(p, "*", SearchOption.AllDirectories))
                {
                    var ext = Path.GetExtension(f).ToLowerInvariant();
                    if (ext is ".wav" or ".mp3" or ".ogg" or ".flac") audioN++;
                }
            }
            catch { /* 忽略 */ }
            break;
        }

        var weights = new List<string>();
        foreach (var pat in new[]
                 {
                     (Path.Combine(Paths.StudioRoot, "audio-sep", "models"), "*.pt"),
                     (Path.Combine(Paths.StudioRoot, "models"), "*.pt"),
                     (Path.Combine(Paths.StudioRoot, "audio-sep"), "onset*.pt"),
                 })
        {
            try
            {
                if (!Directory.Exists(pat.Item1)) continue;
                foreach (var w in Directory.GetFiles(pat.Item1, pat.Item2, SearchOption.AllDirectories))
                    if (!weights.Contains(w)) weights.Add(w);
            }
            catch { /* 忽略 */ }
        }

        var gpuOk = false;
        var gpuName = "";
        var gpuVram = "";
        try
        {
            var psi = new ProcessStartInfo("nvidia-smi", "--query-gpu=name,memory.total --format=csv,noheader")
            {
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var pr = Process.Start(psi);
            if (pr is not null)
            {
                var outText = pr.StandardOutput.ReadToEnd();
                pr.WaitForExit(6000);
                var line = outText.Trim().Split('\n').FirstOrDefault();
                if (pr.ExitCode == 0 && !string.IsNullOrWhiteSpace(line))
                {
                    var parts = line.Split(',').Select(s => s.Trim()).ToArray();
                    gpuOk = true;
                    gpuName = parts.Length > 0 ? parts[0] : "";
                    gpuVram = parts.Length > 1 ? parts[1] : "";
                }
            }
        }
        catch (Exception ex)
        {
            Log.Write("traininfo: nvidia-smi 查询失败 " + ex.Message);
        }

        var data = new Dictionary<string, object?>
        {
            ["script"] = script,
            ["script_ok"] = script.Length > 0,
            ["script_candidates"] = TrainCandidates,
            ["data_dir"] = dataDir,
            ["audio_count"] = audioN,
            ["data_candidates"] = TrainDataCandidates,
            ["weights"] = weights,
            ["gpu"] = new { ok = gpuOk, name = gpuName, vram = gpuVram },
            ["py"] = Paths.PythonExe,
        };
        return new { type = "traininfo", data };
    }

    // ---------- ⑥ 清理临时文件 ----------

    public static object CleanWork()
    {
        long files = 0, bytes = 0;
        try
        {
            foreach (var d in new[] { WorkRoot, Path.Combine(Paths.OutputDir, ".tmp") })
            {
                if (!Directory.Exists(d)) continue;
                foreach (var f in Directory.EnumerateFiles(d, "*", SearchOption.AllDirectories))
                {
                    try { bytes += new FileInfo(f).Length; files++; } catch { /* 忽略 */ }
                }
                try { Directory.Delete(d, recursive: true); } catch (Exception ex) { Log.Write($"清理 {d} 失败: {ex.Message}"); }
            }
        }
        catch (Exception ex)
        {
            Log.Write("CleanWork 失败: " + ex.Message);
        }
        var text = $"已清理 {files} 个临时文件，释放 " +
                   (bytes >= 1024 * 1024 ? $"{bytes / 1024.0 / 1024.0:F1} MB" : $"{bytes / 1024} KB");
        Log.Write("cleanwork: " + text);
        return new { type = "cleaned", files, bytes, text };
    }
}
