using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.Json;

namespace AdofaiStudio.Services;

/// <summary>
/// chartgen sidecar 的进程管理 —— 对齐老壳 Python 侧 backend.py 的
/// is_healthy() / _free_port() / start() / stop()。
///
/// Adofai-Chart-Generator引擎是 Python（我们不重写它，按路线 B 当后台黑盒），我们只负责
/// 「起得来、带对的环境变量、别弹黑框、别把临时文件写到 C 盘」。
/// </summary>
public static class Sidecar
{
    private static Process? _proc;
    private static readonly object _sync = new();

    /// <summary>
    /// 🔴 2026-10-04 运行时融合：chartgen 不再自带 Python，改用 audio-sep 的那一份
    ///    （chartgen/runtime 已删）。解释器路径统一取 Paths.PythonExe，别在此处另拼。
    /// </summary>
    public static string PythonExe => Paths.PythonExe;

    /// <summary>
    /// 🔴 临时目录一律放 D 盘。Adofai-Chart-Generator预览音频用 tempfile.mkdtemp() 落盘，
    ///    不注入 TMP/TEMP 就会写进 C:\Users\...\Temp —— 违反"绝不写 C 盘"铁律。
    /// </summary>
    public static string TmpDir => Path.Combine(Paths.OutputDir, ".tmp");

    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };

    public static bool IsHealthy()
    {
        try
        {
            using var resp = Http.GetAsync($"http://127.0.0.1:{Paths.SidecarPort}/api/health")
                .GetAwaiter().GetResult();
            if (!resp.IsSuccessStatusCode) return false;
            var txt = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            using var doc = JsonDocument.Parse(txt);
            return doc.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// 等 sidecar 变健康（给页面用）。
    /// 🔴 必需：sidecar 是 Python 冷启动，要 3~5 秒；而页面是"秒开"的，
    ///    它在第 40 毫秒就发 get_schema —— 直接去取会扑空（实测就是
    ///    "由于目标计算机积极拒绝，无法连接 127.0.0.1:8765"）。
    /// </summary>
    public static bool WaitHealthy(int seconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            if (IsHealthy()) return true;
            Thread.Sleep(400);
        }
        return IsHealthy();
    }

    /// <summary>
    /// 杀掉占用指定端口的进程（清上次会话残留）。
    /// 老壳注释里的原因：sidecar 是长驻进程，上次会话没干净退出就会一直占着端口，
    /// 而那个老进程是「没带 TMP/TEMP 重定向」起出来的，预览音会写进 C 盘。
    /// </summary>
    public static void FreePort(int port)
    {
        var pid = Gateway.FindListenerPid(port);
        if (pid is null) return;
        Log.Write($"端口 {port} 被 pid={pid}（{Gateway.ProcessNameOf(pid.Value)}）占用，清掉它");
        Gateway.KillPid(pid.Value);
    }

    /// <summary>启动并等就绪。幂等：自己起的且仍健康就直接返回。</summary>
    public static bool Start(int timeoutSeconds = 120)
    {
        lock (_sync)
        {
            if (_proc is null && IsHealthy())
            {
                // 端口被别人占了（上次会话残留）：那种进程没带 TMP 重定向，
                // 必须先清掉再起一个干净的 —— 否则预览音会落进 C 盘。
                Log.Write("检测到非本会话的 sidecar 在跑，清掉后起带 TMP 重定向的干净进程");
                FreePort(Paths.SidecarPort);
                Thread.Sleep(1000);
            }
            if (IsHealthy())
            {
                Log.Write("✓ sidecar 已就绪（复用现有进程）");
                return true;
            }

            if (_proc is not null)
            {
                try { _proc.Kill(); } catch { /* 忽略 */ }
                _proc = null;
            }

            if (!File.Exists(PythonExe))
                throw new FileNotFoundException("找不到 Python 运行时（audio-sep/runtime）: " + PythonExe);

            Directory.CreateDirectory(TmpDir);

            var psi = new ProcessStartInfo(PythonExe)
            {
                WorkingDirectory = Paths.ChartgenDir,
                UseShellExecute = false,
                CreateNoWindow = true,                             // 别弹控制台黑框
                RedirectStandardOutput = false,
                RedirectStandardError = false,
            };
            psi.ArgumentList.Add("-m");
            psi.ArgumentList.Add("sidecar.server");
            psi.ArgumentList.Add("--port");
            psi.ArgumentList.Add(Paths.SidecarPort.ToString());
            // ★ --root 显式给出仓库根（core/ patterns/ samples/ 都在这）——
            //   Adofai-Chart-Generator主进程也是这么起的，只靠 cwd 推断在某些启动方式下会踩空。
            psi.ArgumentList.Add("--root");
            psi.ArgumentList.Add(Paths.ChartgenDir);

            var env = psi.Environment;
            env["PYTHONPATH"] = Paths.ChartgenDir + Path.PathSeparator + (env.TryGetValue("PYTHONPATH", out var old) ? old : "");
            env["TMP"] = TmpDir;
            env["TEMP"] = TmpDir;
            env["TMPDIR"] = TmpDir;

            _proc = Process.Start(psi);
            Log.Write($"sidecar 已拉起 pid={_proc?.Id}（TMP={TmpDir}）");

            var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
            while (DateTime.UtcNow < deadline)
            {
                if (IsHealthy())
                {
                    Log.Write("✓ sidecar 就绪");
                    return true;
                }
                if (_proc is { HasExited: true })
                    throw new InvalidOperationException($"sidecar 启动即退出（exit={_proc.ExitCode}）");
                Thread.Sleep(500);
            }
            throw new TimeoutException($"sidecar 启动超时（{timeoutSeconds}s 内未就绪）");
        }
    }

    /// <summary>后台起（不阻塞 UI 线程）；失败只记日志，不弹窗。</summary>
    public static void StartInBackground()
    {
        var t = new Thread(() =>
        {
            try { Start(); }
            catch (Exception ex) { Log.Write("【sidecar 启动失败】" + ex.Message); }
        })
        { IsBackground = true, Name = "sidecar-start" };
        t.Start();
    }

    public static void Stop()
    {
        lock (_sync)
        {
            if (_proc is null) return;
            try { _proc.Kill(); } catch { /* 忽略 */ }
            _proc = null;
            Log.Write("sidecar 已停止");
        }
    }
}
