using System.Reflection;
using System.Threading;
using System.Windows;
using System.Windows.Threading;

namespace AdofaiStudio;

public partial class App : Application
{
    private const string MutexName = @"Local\AdofaiStudio.SingleInstance";

    private Mutex? _instance;

    protected override void OnStartup(StartupEventArgs e)
    {
        // 单实例：对应 Electron 的 requestSingleInstanceLock()。
        // 已有实例时不弹第二个窗口，直接退出（避免两个壳同时抢 8766 端口）。
        _instance = new Mutex(initiallyOwned: true, MutexName, out var isFirst);
        if (!isFirst)
        {
            Log.Write("检测到已有实例在运行，本次启动退出（单实例锁）");
            Shutdown(0);
            return;
        }

        // 错误直显 —— 宁可弹框也不许静默闪退。
        DispatcherUnhandledException += OnDispatcherUnhandled;
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            Log.Write("【严重】未捕获异常: " + args.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            Log.Write("【后台】未观察异常: " + args.Exception);
            args.SetObserved();
        };

        var ver = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "?";
        Log.Write("============ 启动 ADOFAI Studio (C# 壳) v" + ver + " ============");
        Log.Write("日志文件: " + Log.File);

        base.OnStartup(e);
    }

    private void OnDispatcherUnhandled(object? sender, DispatcherUnhandledExceptionEventArgs e)
    {
        Log.Write("【UI 异常】" + e.Exception);
        MessageBox.Show(
            "界面线程出了个错，已经记进日志了：\n\n" + e.Exception.Message +
            "\n\n日志：" + Log.File,
            "ADOFAI Studio", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true; // 不闪退
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Log.Write("============ 退出 ============");
        try
        {
            _instance?.ReleaseMutex();
            _instance?.Dispose();
        }
        catch
        {
            // 忽略：进程都要走了
        }
        base.OnExit(e);
    }
}
