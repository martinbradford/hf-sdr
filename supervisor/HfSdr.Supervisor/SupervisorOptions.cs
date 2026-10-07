namespace HfSdr.Supervisor;

/// <summary>Bound from the "Supervisor" section of supervisor.json. Paths come ONLY from here, never from a message (design §12.5).</summary>
public sealed class SupervisorOptions
{
    public string PythonExe { get; set; } = @"C:\Users\MABY\radioconda\python.exe";
    public string ServerScript { get; set; } = "";
    /// <summary>Interface the supervisor's own endpoint binds ("*" = all).</summary>
    public string ListenAddress { get; set; } = "*";
    public int Port { get; set; } = 5554;
    /// <summary>Passed to server.py --bind. Explicit so the server's exposure is a config decision.</summary>
    public string ServerBind { get; set; } = "*";
    public int ControlPort { get; set; } = 5555;
    public int StreamPort { get; set; } = 5556;
    public int AudioPort { get; set; } = 5557;
    public double DefaultCenterHz { get; set; } = 7.15e6;
    public int StartTimeoutSec { get; set; } = 20;
    public int ModeSwitchTimeoutSec { get; set; } = 60;
    public int GracefulStopSec { get; set; } = 5;
}
