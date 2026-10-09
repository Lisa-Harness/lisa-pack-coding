# Leasewarden project contract

## Validation

Use the installed Go 1.23 toolchain and standard library only.
Run `go test ./...`, `go vet ./...`, and `gofmt -l .` before handing off.
Do not add dependency downloads, build wrappers, or a `go.sum`.
Read the relevant [Go skill](skills/golang/SKILL.md) reference before editing.

## Architecture map

| File or package | Responsibility |
| --- | --- |
| `main.go` | entrypoint, flags, startup checks, server composition |
| `daemon.go` | exclusive PID file, optional reload signal, shutdown |
| `server.go` | `newRouter(routerDeps)`, routes, JSON plumbing |
| `leases.go` | normalized read-only lease snapshots |
| `lease_handlers.go` | leases, clients, address and device queries |
| `hostdir.go` | persistent reservation files and Store operations |
| `reservations.go` | HTTP write validation and publish sequencing |
| `validation.go` | IPv4 subnet and hostname rules |
| `token*.go` | fixed-token bearer authentication |
| `cmd/` | terminal commands and environment/flag precedence |
| `internal/client/` | bounded HTTP transport and structured failures |
| `internal/macs/` | canonical device keys and filenames |
| `internal/output/` | table and JSON presentation |

## Working boundaries

The root package owns domain behavior; client packages cannot import `main`.
Use the existing router constructor in handler tests rather than duplicate routes.
Keep the public routes and JSON fields consistent with README and CLI examples.
Do not import a web framework, SQL driver, CLI framework, or assertion library.
Avoid unrelated refactors and preserve the single-binary dispatcher.

Files in hostsdir use bare comma-separated records, not config directives.
Validate before writing and publish only after a successful durable mutation.
The Store is file-backed, not an in-memory source of truth.
Never write the lease file, log tokens, or silently retry mutations.
