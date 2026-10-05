using System; using System.Reflection; using System.Linq;
class P {
    static void Main() {
        // 检查 CPU 包托管程序集是否含 OrtCUDAProviderOptions
        // 包目录优先取 NUGET_PACKAGES 环境变量，否则默认 %USERPROFILE%\.nuget\packages
        var nugetRoot = Environment.GetEnvironmentVariable("NUGET_PACKAGES");
        if (string.IsNullOrEmpty(nugetRoot))
            nugetRoot = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".nuget", "packages");
        var cpuManaged = System.IO.Path.Combine(nugetRoot,
            @"microsoft.ml.onnxruntime\1.30.0\lib\netstandard2.1\Microsoft.ML.OnnxRuntime.dll");
        if (!System.IO.File.Exists(cpuManaged)) {
            // 试其他子目录
            var ortLib = System.IO.Path.Combine(nugetRoot, @"microsoft.ml.onnxruntime\1.30.0\lib");
            foreach (var d in System.IO.Directory.GetDirectories(ortLib))
                foreach (var f in System.IO.Directory.GetFiles(d,"Microsoft.ML.OnnxRuntime.dll"))
                    cpuManaged=f;
        }
        Console.WriteLine("CPU 托管:", cpuManaged);
        var asm = Assembly.LoadFrom(cpuManaged);
        var t = asm.GetType("Microsoft.ML.OnnxRuntime.OrtCUDAProviderOptions");
        Console.WriteLine("含 OrtCUDAProviderOptions ?", t!=null);
        // 该类型的 Append 方法
        if (t!=null)
            foreach (var m in typeof(Microsoft.ML.OnnxRuntime.SessionOptions).GetMethods().Where(m=>m.Name.Contains("CUDA")))
                Console.WriteLine("  SessionOptions."+m.Name);
    }
}
