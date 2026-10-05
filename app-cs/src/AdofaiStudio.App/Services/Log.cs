using System.IO;
using System.Text;

namespace AdofaiStudio;

/// <summary>
/// 极简日志：所有排查都靠它，落盘一律在 D 盘（项目铁律：绝不写 C 盘）。
/// 对应 Python 老壳里的 _dbg()。
/// </summary>
public static class Log
{
    private static readonly object Gate = new();

    /// <summary>日志目录：&lt;studio 根&gt;\app-cs\logs —— 无论 exe 被拷到哪都落得回来。</summary>
    public static string Dir { get; } = Path.Combine(Paths.StudioRoot, "app-cs", "logs");

    public static string File => Path.Combine(Dir, "app.log");

    public static void Write(string message)
    {
        var line = $"[{DateTime.Now:HH:mm:ss.fff}] {message}";
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Dir);
                System.IO.File.AppendAllText(File, line + Environment.NewLine, Encoding.UTF8);
            }
        }
        catch
        {
            // 日志本身不能把程序搞崩
        }
        Console.WriteLine(line);
    }
}
