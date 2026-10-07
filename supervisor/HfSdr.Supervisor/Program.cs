using HfSdr.Supervisor;

var builder = Host.CreateApplicationBuilder(args);
// A service starts in System32, so resolve config next to the exe, not the CWD.
builder.Configuration.AddJsonFile(Path.Combine(AppContext.BaseDirectory, "supervisor.json"), optional: false, reloadOnChange: false);
builder.Services.AddWindowsService(o => o.ServiceName = "HfSdrSupervisor");
builder.Services.Configure<SupervisorOptions>(builder.Configuration.GetSection("Supervisor"));
builder.Services.AddSingleton<ServerHost>();
builder.Services.AddHostedService<SupervisorWorker>();
builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(25));
builder.Build().Run();
