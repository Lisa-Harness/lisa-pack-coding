# Build and process operations

## Offline module

The module path is `example.invalid/leasewarden`.
The go directive is `1.23.0`.
All dependencies are standard-library packages or this module's packages.
A `go.sum` is not needed.
CGO is not required.
No code generator, package download, or container is part of the build.
Use the installed Go toolchain in constrained environments.

## Standard checks

```sh
go test ./...
go vet ./...
gofmt -l .
```

A formatting check must print nothing.
Tests can populate the user's Go build cache.
If a sandbox requires a workspace cache, keep it ignored.
`.lisa-go-cache/` and `.lisa-go-path/` are reserved ignored locations.
Do not commit a cache, binary, or test scratch directory.

## Build output

```sh
go build -o dist/leasewarden .
./dist/leasewarden --help
./dist/leasewarden server --help
./dist/leasewarden version
```

Use `dist/` for local build products.
The version defaults to `development`.
The entrypoint's version variable can be set with Go linker flags.
There is no release wrapper to keep synchronized.
Do not introduce one as part of a handler change.

## Startup validation

The service requires a nonempty token in the environment.
The lease path and hosts directory must be nonempty.
The listen address must have host and port syntax.
The subnet must be an IPv4 CIDR.
The shutdown timeout must be positive.
The lease path must exist as a readable regular file at startup.
The hosts directory is created when necessary.
An invalid configuration must fail before serving requests.
Do not silently fall back to an unrestricted subnet.

## Foreground supervision

The process stays in the foreground.
It is suitable for a service manager or shell supervision.
There is no double-fork daemon protocol.
There are no inherited-listener environment variables.
There is no privilege drop implemented by Leasewarden.
Grant the service account the minimum filesystem and signaling permissions.
The default listener is loopback.
Use a reverse proxy when exposing the API outside the host.

## PID files

`--pid-file` is optional.
When set, the file is created exclusively.
The file records the current Leasewarden process ID.
Normal shutdown removes the claimed file.
An existing file is an error rather than an overwrite.
A killed process may leave a stale file.
Operators must inspect stale ownership before removing it.
Do not delete another service's PID file during startup.
Do not assume a process ID can never be reused.

## Dnsmasq reload

`--dnsmasq-pid` is independent from Leasewarden's PID file.
Without it, the Publisher is a no-op for directory-monitoring setups.
With it, each completed mutation reads the current PID file.
Only positive process IDs greater than one are accepted.
Publish sends SIGHUP to that specific process.
A read, parse, or signal error is visible to the HTTP caller.
Publication is synchronous; there is no hidden retry worker.
The changed file remains durable after a reload failure.

## Shutdown

SIGINT and SIGTERM cancel the process context.
The server attempts graceful shutdown with a fresh bounded context.
That context must not already be cancelled.
If draining fails, close the server to stop active connections.
The serve goroutine reports through a buffered result channel.
Wait for its result rather than leaking the goroutine.
An expected `http.ErrServerClosed` is not a failure.
PID cleanup runs when the server function returns.

## Troubleshooting sequence

1. Check the configured lease file is readable.
2. Check the hosts directory is writable by the service account.
3. Confirm the managed subnet matches the lab network.
4. Request `/health` to check HTTP reachability.
5. Request an authenticated read to check token configuration.
6. Inspect managed record contents after a mutation.
7. Inspect the dnsmasq PID path after a publish failure.
8. Check service-manager logs for startup errors.

Do not include tokens in copied command output or incident notes.
Do not test reload by signaling a process chosen only by name.
Do not remove production configuration to repair a local build.
