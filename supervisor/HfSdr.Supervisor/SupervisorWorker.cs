using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Options;
using NetMQ;
using NetMQ.Sockets;

namespace HfSdr.Supervisor;

/// <summary>REP endpoint (default :5554): status / start / stop. Same envelope as protocol/messages.md.</summary>
public sealed class SupervisorWorker(ServerHost host, IOptions<SupervisorOptions> options, ILogger<SupervisorWorker> log) : BackgroundService
{
    private readonly SupervisorOptions _opt = options.Value;

    protected override Task ExecuteAsync(CancellationToken stoppingToken) =>
        Task.Factory.StartNew(() => Loop(stoppingToken), stoppingToken, TaskCreationOptions.LongRunning, TaskScheduler.Default);

    private void Loop(CancellationToken ct)
    {
        string addr = $"tcp://{_opt.ListenAddress}:{_opt.Port}";
        using var rep = new ResponseSocket();
        try { rep.Bind(addr); }
        catch (Exception ex) { log.LogCritical(ex, "cannot bind {Addr}", addr); throw; }
        log.LogInformation("supervisor listening on {Addr}", addr);

        while (!ct.IsCancellationRequested)
        {
            if (!rep.TryReceiveFrameString(TimeSpan.FromMilliseconds(250), out var text)) continue;
            rep.SendFrame(Handle(text).ToJsonString());
        }
    }

    private JsonObject Handle(string text)
    {
        JsonNode? id = null;
        try
        {
            var req = JsonNode.Parse(text)?.AsObject() ?? throw new BadRequestException("request must be a JSON object");
            id = req["id"]?.DeepClone();
            string cmd = req["cmd"]?.GetValue<string>() ?? throw new BadRequestException("missing cmd");
            var p = req["params"] as JsonObject;
            // req["auth"] is reserved (design §12.5): ignored until a secret is configured.

            JsonObject result = cmd switch
            {
                "status" => host.Status(),
                "start" => host.Start(p),
                "stop" => host.Stop(),
                _ => throw new BadRequestException($"unknown command: {cmd}"),
            };
            return new JsonObject { ["id"] = id, ["ok"] = true, ["result"] = result };
        }
        catch (Exception ex) when (ex is BadRequestException or JsonException or InvalidOperationException or FormatException)
        {
            return new JsonObject
            {
                ["id"] = id, ["ok"] = false,
                ["error"] = new JsonObject { ["code"] = "bad_request", ["message"] = ex.Message },
            };
        }
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        await base.StopAsync(cancellationToken);
        log.LogInformation("service stopping: shutting the server down gracefully");
        await host.ShutdownAsync();
    }
}
