using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace AdofaiStudio.Services;

/// <summary>
/// 同源网关 —— 对齐老壳 Python 侧 backend.py 的 _GwHandler / _GwServer。
///
/// 为什么要"一个端口干两件事"：前端是 ES module + fetch + SSE，而 sidecar
/// **不发** Access-Control-Allow-Origin，跨端口 fetch 会被 Chromium 拦死。
/// 所以让静态资源与 API 天然同源，零 CORS：
///   ① 静态：/               → gui/index.html（我们的导入页）
///           /workbench/*     → gui/workbench/*（自有工作台）
///           /media_local     → 受控本地媒体（带 Range，白名单限项目目录）
///   ② 反代：/api/* /media /ws → 127.0.0.1:8765（含 SSE 长连接）
///
/// 🔴 用 TcpListener 手写 HTTP/1.1，而不是 .NET 的 HttpListener ——
///    HttpListener 在 Windows 上对任何 http:// 前缀都要求管理员权限
///    （或预先注册 URL ACL），用户双击 exe 会直接抛 AccessDenied。
/// </summary>
public sealed class Gateway : IDisposable
{
    private readonly object _sync = new();
    private TcpListener? _listener;
    private volatile bool _running;

    /// <summary>实际监听的端口（8766 被占时会退到随机空闲端口）。</summary>
    public int Port { get; private set; }

    public string BaseUrl => $"http://127.0.0.1:{Port}";

    private static string GuiDir => Paths.GuiDir;

    // ---------------------------------------------------------------- 生命周期

    public int Start(int preferredPort)
    {
        lock (_sync)
        {
            if (_listener is not null) return Port;

            var listener = TryBind(preferredPort);
            if (listener is null)
            {
                // 端口被"非本程序"的进程占着（比如主人还开着老壳）—— 不去杀别人，
                // 退到随机端口，功能照旧，只是地址变了。
                Log.Write($"网关端口 {preferredPort} 被占用，且占用者不是本程序，改用随机端口");
                listener = TryBind(0);
            }
            if (listener is null)
                throw new IOException($"网关无法监听（尝试端口 {preferredPort} 与随机端口都失败）");

            _listener = listener;
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            _running = true;

            var accept = new Thread(AcceptLoop) { IsBackground = true, Name = "gateway-accept" };
            accept.Start();

            if (!Directory.Exists(GuiDir)) Log.Write($"⚠ 网关静态根不存在: {GuiDir}");

            // 【媒体白名单尾斜杠已修】IsInsideProject 现在对 StudioRoot 做 TrimEnd，/media_local 不再误报 403
            Log.Write("【媒体白名单尾斜杠已修】IsInsideProject 现在对 StudioRoot 做 TrimEnd，/media_local 不再误报 403");

            Log.Write($"✓ 网关已启动 {BaseUrl}（静态根 {GuiDir}）");
            return Port;
        }
    }

    /// <summary>
    /// 先试目标端口；若被占，仅当占用者是本程序自己的残留进程（AdofaiStudio*）
    /// 才清掉重试 —— 绝不动主人正在用的老壳（Python）。
    /// </summary>
    private static TcpListener? TryBind(int port)
    {
        try
        {
            var l = new TcpListener(IPAddress.Loopback, port);
            l.Start();
            return l;
        }
        catch (SocketException)
        {
            if (port == 0) return null;
            var pid = FindListenerPid(port);
            var name = pid is null ? "" : ProcessNameOf(pid.Value);
            if (name.StartsWith("AdofaiStudio", StringComparison.OrdinalIgnoreCase))
            {
                Log.Write($"端口 {port} 被上次残留的本程序进程占用（pid={pid}），清掉后重试");
                KillPid(pid!.Value);
                Thread.Sleep(600);
                try
                {
                    var l = new TcpListener(IPAddress.Loopback, port);
                    l.Start();
                    return l;
                }
                catch (SocketException) { return null; }
            }
            return null;
        }
    }

    public void Dispose()
    {
        _running = false;
        try { _listener?.Stop(); } catch { /* 忽略 */ }
        _listener = null;
    }

    // ---------------------------------------------------------------- 接受连接

    private void AcceptLoop()
    {
        var listener = _listener;
        if (listener is null) return;
        while (_running)
        {
            TcpClient client;
            try
            {
                client = listener.AcceptTcpClient();
            }
            catch (Exception)
            {
                if (!_running) return;
                continue;
            }
            // 每个连接一条线程：本机单用户场景，连接数是个位数。
            // 阻塞式读写在独立线程里最省心（SSE 要长时间占住一个连接）。
            var t = new Thread(() => { try { Handle(client); } catch (Exception ex) { Log.Write("网关连接异常: " + ex.Message); } finally { try { client.Close(); } catch { } } })
            { IsBackground = true, Name = "gateway-conn" };
            t.Start();
        }
    }

    private void Handle(TcpClient client)
    {
        var stream = client.GetStream();
        var req = ReadRequest(stream);
        if (req is null) return;

        var rawPath = req.Target;
        var q = rawPath.IndexOf('?');
        var encodedPath = q >= 0 ? rawPath[..q] : rawPath;
        var query = q >= 0 ? rawPath[(q + 1)..] : "";
        string path;
        try { path = Uri.UnescapeDataString(encodedPath); } catch { path = encodedPath; }

        if (path == "/media_local")
        {
            ServeMediaLocal(stream, query, req);
            return;
        }
        if (IsApi(path))
        {
            Proxy(stream, req);
            return;
        }
        if (req.Method == "GET" || req.Method == "HEAD")
        {
            ServeStatic(stream, path, req.IsHead);
            return;
        }
        SendJson(stream, 404, "{\"ok\":false,\"error\":\"未知路径\"}");
    }

    private static bool IsApi(string path) =>
        path.StartsWith("/api/", StringComparison.Ordinal) || path == "/media" || path == "/ws";

    // ---------------------------------------------------------------- 静态文件

    /// <summary>
    /// 静态根就是 gui/ 一个（导入页 + workbench/ 同源伺服）。
    /// （2026-10-04 起 /studio/* 与 gui/studio/ 已整体删除，不再有第二静态根）
    /// </summary>
    private void ServeStatic(Stream stream, string path, bool head)
    {
        string baseDir = GuiDir;
        var rel = path.TrimStart('/');

        baseDir = Path.GetFullPath(baseDir);
        var target = Path.GetFullPath(Path.Combine(baseDir, rel.Replace('/', Path.DirectorySeparatorChar)));
        // 挡目录穿越：解析后必须仍在根内
        if (target != baseDir && !target.StartsWith(baseDir + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            target = baseDir;

        if (Directory.Exists(target))
            target = Path.Combine(target, "index.html");

        if (!File.Exists(target))
        {
            // favicon 的 404 是正常的（老壳也一样），别刷屏
            if (!path.EndsWith("favicon.ico", StringComparison.OrdinalIgnoreCase))
                Log.Write($"网关 404: {path}（解析到 {target}）");
            SendText(stream, 404, "404 Not Found: " + path);
            return;
        }

        try
        {
            var fi = new FileInfo(target);
            var head_ = new Response(stream, 200, "OK");
            head_.Header("Content-Type", MimeOf(target));
            head_.Header("Content-Length", fi.Length.ToString());
            head_.Header("Accept-Ranges", "bytes");
            head_.Header("Cache-Control", "no-store");
            head_.Header("Connection", "close");
            head_.EndHeaders();
            if (!head)
            {
                using var fs = File.OpenRead(target);
                fs.CopyTo(stream);
            }
        }
        catch (Exception ex)
        {
            Log.Write($"网关读文件失败 {target}: {ex.Message}");
            try { SendText(stream, 500, "500 " + ex.Message); } catch { }
        }
    }

    // ------------------------------------------------- 受控本地媒体（带 Range）

    /// <summary>
    /// 只允许伺服「项目目录内」的真实文件，并支持 Range（&lt;audio&gt; 拖进度必需）。
    /// 老壳注释里的原因：sidecar 的 /media 有白名单，output/.work 下的分轨 wav 会被拒，
    /// 所以这里单独开一条同源通道。
    /// </summary>
    private void ServeMediaLocal(Stream stream, string query, Request req)
    {
        var head = req.IsHead;
        string? p = null;
        foreach (var kv in query.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var i = kv.IndexOf('=');
            if (i > 0 && kv[..i] == "path")
            {
                try { p = Uri.UnescapeDataString(kv[(i + 1)..].Replace("+", " ")); } catch { p = kv[(i + 1)..]; }
                break;
            }
        }

        if (string.IsNullOrWhiteSpace(p) || !File.Exists(p) || !IsInsideProject(p))
        {
            SendJson(stream, 403, "{\"ok\":false,\"error\":\"非法或不存在的路径（仅限本项目目录）\"}");
            return;
        }

        try
        {
            var fp = Path.GetFullPath(p);
            var size = new FileInfo(fp).Length;
            var ctype = MimeOf(fp);
            var range = req.Headers.TryGetValue("Range", out var rg) ? rg : null;

            long start = 0, end = size - 1;
            var partial = false;
            if (!string.IsNullOrEmpty(range) && range.StartsWith("bytes=", StringComparison.Ordinal))
            {
                var spec = range["bytes=".Length..].Split(',')[0].Trim();
                var dash = spec.IndexOf('-');
                if (dash >= 0)
                {
                    var a = spec[..dash];
                    var b = spec[(dash + 1)..];
                    start = string.IsNullOrEmpty(a) ? 0 : long.Parse(a);
                    end = string.IsNullOrEmpty(b) ? size - 1 : long.Parse(b);
                    if (end >= size) end = size - 1;
                    if (start > end) start = 0;
                    partial = true;
                }
            }

            var length = partial ? end - start + 1 : size;
            var r = new Response(stream, partial ? 206 : 200, partial ? "Partial Content" : "OK");
            r.Header("Content-Type", ctype);
            r.Header("Accept-Ranges", "bytes");
            if (partial) r.Header("Content-Range", $"bytes {start}-{end}/{size}");
            r.Header("Content-Length", length.ToString());
            r.Header("Cache-Control", "no-store");
            r.Header("Connection", "close");
            r.EndHeaders();
            if (head) return;

            using var fs = File.OpenRead(fp);
            if (partial) fs.Seek(start, SeekOrigin.Begin);
            var buf = new byte[64 * 1024];
            var left = length;
            while (left > 0)
            {
                var n = fs.Read(buf, 0, (int)Math.Min(buf.Length, left));
                if (n <= 0) break;
                stream.Write(buf, 0, n);
                left -= n;
            }
            stream.Flush();
        }
        catch (Exception ex)
        {
            Log.Write($"网关读媒体失败 {p}: {ex.Message}");
        }
    }

    private static bool IsInsideProject(string abspath)
    {
        try
        {
            var target = Path.GetFullPath(abspath);
            // 🔴 启动器 TrimEnd 会故意把尾反斜杠加回（StudioRoot = "<REPO>\"），
            // 此时 root + Path.DirectorySeparatorChar 会变成双反斜杠，StartsWith 永远 false ⇒ 全部 403。
            // 这里对 root 先做 TrimEnd 再拼，对任意启动来源都健壮（便携包 ≠ dev 树，铁律 13）。
            var root = Path.GetFullPath(Paths.StudioRoot)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            return target.Equals(root, StringComparison.OrdinalIgnoreCase) ||
                   target.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    // ---------------------------------------------------------------- 反代

    private void Proxy(Stream stream, Request req)
    {
        var rawPath = req.Target.Split('?')[0];
        if (rawPath == "/ws")
        {
            // BDG 桥的 WebSocket 升级要走裸隧道，暂不做（不影响出谱主链路）
            SendJson(stream, 501, "{\"ok\":false,\"error\":\"网关暂不支持 WebSocket\"}");
            return;
        }

        try
        {
            using var up = new TcpClient();
            up.Connect(IPAddress.Loopback, Paths.SidecarPort);
            using var us = up.GetStream();
            // sidecar 处理慢（跑推理可达数分钟），别让它被本端超时掐断
            up.ReceiveTimeout = 0;
            up.SendTimeout = 0;

            var sb = new StringBuilder();
            sb.Append(req.Method).Append(' ').Append(req.Target).Append(" HTTP/1.1\r\n");
            sb.Append("Host: 127.0.0.1:").Append(Paths.SidecarPort).Append("\r\n");
            foreach (var kv in req.Headers)
            {
                var k = kv.Key.ToLowerInvariant();
                if (k is "host" or "connection" or "accept-encoding" or "content-length" or "transfer-encoding")
                    continue;
                sb.Append(kv.Key).Append(": ").Append(kv.Value).Append("\r\n");
            }
            sb.Append("Content-Length: ").Append(req.Body?.Length ?? 0).Append("\r\n");
            sb.Append("Connection: close\r\n\r\n");
            var headBytes = Encoding.ASCII.GetBytes(sb.ToString());
            us.Write(headBytes, 0, headBytes.Length);
            if (req.Body is { Length: > 0 })
                us.Write(req.Body, 0, req.Body.Length);
            us.Flush();

            // ---- 读上游响应头 ----
            var headerBlock = ReadHeaderBlock(us);
            if (headerBlock is null)
            {
                SendJson(stream, 502, "{\"ok\":false,\"error\":\"sidecar 无响应\"}");
                return;
            }
            var (status, reason, headers) = ParseResponseHead(headerBlock);

            var ctype = headers.TryGetValue("content-type", out var ct) ? ct : "";
            long? clen = null;
            if (headers.TryGetValue("content-length", out var cl) && long.TryParse(cl.Trim(), out var n))
                clen = n;
            var chunked = headers.TryGetValue("transfer-encoding", out var te) &&
                          te.Contains("chunked", StringComparison.OrdinalIgnoreCase);
            var isSse = ctype.Contains("text/event-stream", StringComparison.OrdinalIgnoreCase);

            var resp = new Response(stream, status, reason);
            foreach (var kv in headers)
            {
                var k = kv.Key;
                // 这三个由我们自己重新决定；其余原样透传（含 Content-Range / Content-Type）
                if (k is "transfer-encoding" or "connection" or "content-length" or "keep-alive")
                    continue;
                resp.Header(kv.Key, kv.Value);
            }
            if (clen is not null) resp.Header("Content-Length", clen.Value.ToString());
            if (isSse) resp.Header("Cache-Control", "no-cache");
            if (clen is null) resp.Header("Connection", "close");
            resp.EndHeaders();

            if (req.IsHead) return;

            // ---- 转发响应体 ----
            // 🔴 SSE 必须逐段读、逐段 flush —— 绝不能等 read() 收完（那是长连接，
            //    等下去界面就永远不动，进度条会卡死在第一步）。
            var buf = new byte[16 * 1024];
            var decoder = chunked ? new ChunkedReader(us) : null;
            while (true)
            {
                var read = decoder is null
                    ? us.Read(buf, 0, buf.Length)
                    : decoder.Read(buf, 0, buf.Length);
                if (read <= 0) break;
                stream.Write(buf, 0, read);
                stream.Flush();
                if (clen is not null)
                {
                    clen -= read;
                    if (clen <= 0) break;
                }
            }
            stream.Flush();
        }
        catch (Exception ex)
        {
            Log.Write($"反代失败 {req.Method} {req.Target}: {ex.Message}");
            try { SendJson(stream, 502, "{\"ok\":false,\"error\":\"sidecar 不可达\"}"); } catch { }
        }
    }

    /// <summary>把 chunked 帧拆掉，只吐负载字节（对客户端用 Connection: close 收尾）。</summary>
    private sealed class ChunkedReader
    {
        private readonly Stream _s;
        private int _remaining;
        private bool _done;

        public ChunkedReader(Stream s) => _s = s;

        public int Read(byte[] buf, int off, int count)
        {
            if (_done) return 0;
            while (_remaining == 0)
            {
                var line = ReadLine(_s);
                if (line is null) { _done = true; return 0; }
                var hex = line.Trim().Split(';')[0];
                if (hex.Length == 0) continue;
                if (!int.TryParse(hex, System.Globalization.NumberStyles.HexNumber, null, out var size))
                {
                    _done = true;
                    return 0;
                }
                if (size == 0)
                {
                    // 末尾：吃掉结尾的 CRLF
                    ReadLine(_s);
                    _done = true;
                    return 0;
                }
                _remaining = size;
            }
            var want = Math.Min(count, _remaining);
            var n = _s.Read(buf, off, want);
            if (n <= 0) { _done = true; return 0; }
            _remaining -= n;
            if (_remaining == 0) ReadLine(_s);   // 块尾 CRLF
            return n;
        }
    }

    // ---------------------------------------------------------------- 请求解析

    private sealed class Request
    {
        public string Method = "GET";
        public string Target = "/";
        public Dictionary<string, string> Headers = new(StringComparer.OrdinalIgnoreCase);
        public byte[]? Body;
        public bool IsHead => Method == "HEAD";
    }

    private static Request? ReadRequest(Stream s)
    {
        var headBlock = ReadHeaderBlock(s);
        if (headBlock is null) return null;
        var text = Encoding.UTF8.GetString(headBlock);
        var lines = text.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        if (lines.Length == 0) return null;

        var parts = lines[0].Split(' ');
        if (parts.Length < 3) return null;

        var req = new Request { Method = parts[0].ToUpperInvariant(), Target = parts[1] };
        for (var i = 1; i < lines.Length; i++)
        {
            var idx = lines[i].IndexOf(':');
            if (idx <= 0) continue;
            var k = lines[i][..idx].Trim();
            var v = lines[i][(idx + 1)..].Trim();
            req.Headers[k] = v;
        }

        if (req.Headers.TryGetValue("Content-Length", out var cl) &&
            long.TryParse(cl, out var len) && len > 0)
        {
            var body = new byte[len];
            var got = 0;
            while (got < len)
            {
                var n = s.Read(body, got, (int)(len - got));
                if (n <= 0) break;
                got += n;
            }
            req.Body = body;
        }
        return req;
    }

    /// <summary>
    /// 读到「空行」为止（即 \r\n\r\n），返回不含结尾空行的头块。
    /// 🔴 判定必须看"连续两个换行"，不能只数换行符个数 —— 第一版写成"见到第二个
    ///    换行符就收工"，结果请求行一结束就被截断，整个请求解析全乱。
    /// </summary>
    private static byte[]? ReadHeaderBlock(Stream s)
    {
        using var ms = new MemoryStream();
        var one = new byte[1];
        var tail = new byte[4];
        while (ms.Length < 128 * 1024)
        {
            int n;
            try { n = s.Read(one, 0, 1); }
            catch { return ms.Length > 0 ? ms.ToArray() : null; }
            if (n <= 0) return ms.Length > 0 ? ms.ToArray() : null;
            var b = one[0];

            tail[0] = tail[1]; tail[1] = tail[2]; tail[2] = tail[3]; tail[3] = b;

            if (b == (byte)'\n' &&
                (tail[1] == (byte)'\n' || (tail[2] == (byte)'\r' && tail[1] == (byte)'\n')))
            {
                var arr = ms.ToArray();
                var len = arr.Length;
                while (len > 0 && (arr[len - 1] == (byte)'\r' || arr[len - 1] == (byte)'\n')) len--;
                return arr[..len];
            }
            ms.WriteByte(b);
        }
        return ms.ToArray();
    }

    private static string? ReadLine(Stream s)
    {
        var sb = new StringBuilder();
        var one = new byte[1];
        while (true)
        {
            int n;
            try { n = s.Read(one, 0, 1); } catch { return null; }
            if (n <= 0) return sb.Length > 0 ? sb.ToString() : null;
            var c = (char)one[0];
            if (c == '\n') return sb.ToString();
            if (c != '\r') sb.Append(c);
        }
    }

    private static (int Status, string Reason, Dictionary<string, string> Headers) ParseResponseHead(byte[] block)
    {
        var text = Encoding.UTF8.GetString(block);
        var lines = text.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        var status = 502;
        var reason = "Bad Gateway";
        if (lines.Length > 0)
        {
            var p = lines[0].Split(' ', 3);
            if (p.Length >= 2 && int.TryParse(p[1], out var st)) status = st;
            if (p.Length >= 3) reason = p[2];
        }
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 1; i < lines.Length; i++)
        {
            var idx = lines[i].IndexOf(':');
            if (idx <= 0) continue;
            headers[lines[i][..idx].Trim()] = lines[i][(idx + 1)..].Trim();
        }
        return (status, reason, headers);
    }

    // ---------------------------------------------------------------- 响应工具

    private sealed class Response
    {
        private readonly Stream _s;
        private readonly StringBuilder _sb = new();

        public Response(Stream s, int status, string reason)
        {
            _s = s;
            _sb.Append("HTTP/1.1 ").Append(status).Append(' ').Append(reason).Append("\r\n");
        }

        public void Header(string k, string v) => _sb.Append(k).Append(": ").Append(v).Append("\r\n");

        public void EndHeaders()
        {
            _sb.Append("\r\n");
            var b = Encoding.UTF8.GetBytes(_sb.ToString());
            _s.Write(b, 0, b.Length);
            _s.Flush();
        }
    }

    private static void SendText(Stream s, int code, string text)
    {
        var body = Encoding.UTF8.GetBytes(text);
        var r = new Response(s, code, Reason(code));
        r.Header("Content-Type", "text/plain; charset=utf-8");
        r.Header("Content-Length", body.Length.ToString());
        r.Header("Cache-Control", "no-store");
        r.Header("Connection", "close");
        r.EndHeaders();
        s.Write(body, 0, body.Length);
        s.Flush();
    }

    private static void SendJson(Stream s, int code, string json)
    {
        var body = Encoding.UTF8.GetBytes(json);
        var r = new Response(s, code, Reason(code));
        r.Header("Content-Type", "application/json; charset=utf-8");
        r.Header("Content-Length", body.Length.ToString());
        r.Header("Cache-Control", "no-store");
        r.Header("Connection", "close");
        r.EndHeaders();
        s.Write(body, 0, body.Length);
        s.Flush();
    }

    private static string Reason(int code) => code switch
    {
        200 => "OK",
        206 => "Partial Content",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        _ => "OK",
    };

    // ---------------------------------------------------------------- MIME

    public static string MimeOf(string path) => Path.GetExtension(path).ToLowerInvariant() switch
    {
        ".html" or ".htm" => "text/html; charset=utf-8",
        ".js" or ".mjs" => "text/javascript; charset=utf-8",
        ".css" => "text/css; charset=utf-8",
        ".json" => "application/json; charset=utf-8",
        ".svg" => "image/svg+xml",
        ".png" => "image/png",
        ".jpg" or ".jpeg" => "image/jpeg",
        ".gif" => "image/gif",
        ".webp" => "image/webp",
        ".ico" => "image/x-icon",
        ".woff" => "font/woff",
        ".woff2" => "font/woff2",
        ".ttf" => "font/ttf",
        ".wasm" => "application/wasm",
        ".wav" => "audio/wav",
        ".mp3" => "audio/mpeg",
        ".ogg" => "audio/ogg",
        ".flac" => "audio/flac",
        ".m4a" => "audio/mp4",
        ".mid" or ".midi" => "audio/midi",
        ".adofai" => "application/json; charset=utf-8",
        ".txt" or ".md" => "text/plain; charset=utf-8",
        ".map" => "application/json; charset=utf-8",
        _ => "application/octet-stream",
    };

    // ---------------------------------------------------------------- 小工具

    /// <summary>按端口找监听进程 PID（解析 netstat -ano，中文系统输出是 GBK）。</summary>
    public static int? FindListenerPid(int port)
    {
        try
        {
            var psi = new ProcessStartInfo("netstat", "-ano -p TCP")
            {
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.Latin1,   // 原始字节，逐行只取数字部分
            };
            using var p = Process.Start(psi);
            if (p is null) return null;
            var txt = p.StandardOutput.ReadToEnd();
            p.WaitForExit(15000);
            foreach (var line in txt.Split('\n'))
            {
                if (!line.Contains($":{port} ", StringComparison.Ordinal) || !line.Contains("LISTENING", StringComparison.Ordinal))
                    continue;
                var cols = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (cols.Length >= 1 && int.TryParse(cols[^1], out var pid))
                    return pid;
            }
        }
        catch (Exception ex) { Log.Write("FindListenerPid 失败: " + ex.Message); }
        return null;
    }

    public static string ProcessNameOf(int pid)
    {
        try { return Process.GetProcessById(pid).ProcessName; }
        catch { return ""; }
    }

    public static void KillPid(int pid)
    {
        try
        {
            var psi = new ProcessStartInfo("taskkill", $"/PID {pid} /F")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            Process.Start(psi)?.WaitForExit(15000);
        }
        catch (Exception ex) { Log.Write("KillPid 失败: " + ex.Message); }
    }
}
