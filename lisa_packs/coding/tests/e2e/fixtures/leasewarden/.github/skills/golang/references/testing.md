# Testing without external services

## Baseline commands

```sh
go test ./...
go vet ./...
gofmt -l .
```

Tests require neither dnsmasq nor administrator privileges.
No external library supplies assertions or HTTP fixtures.
Use the installed standard toolchain.
Do not download a test runner.

## Test ownership

| Tests | Concern |
| --- | --- |
| `leases_test.go` | parsing, activity, snapshots, domain validation |
| `hostdir_test.go` | durable CRUD, ownership, corrupt records, concurrency |
| `handlers_test.go` | router, request validation, query and write responses |
| `token_test.go` | scheme parsing, secret comparison, public/private routes |
| `internal/macs/macs_test.go` | accepted spellings and safe filenames |
| `internal/client/client_test.go` | authentication, errors, request behavior |
| `internal/output/output_test.go` | table columns, JSON, output failures |
| `cmd/root_test.go` | command mapping and configuration precedence |

## Start narrow

Reproduce the behavior in its nearest test file.
Use a descriptive `Test...` name.
Use subtests for related input variations.
Prefer concrete records over random data.
Use fixed past and distant-future epochs for lease examples.
Use an explicit clock value when testing `Lease.Active` directly.
Avoid sleeps to assert expiration.

Focused commands:

```sh
go test -run '^TestParseLease$' .
go test -run '^TestReservationHTTP$' .
go test ./internal/macs ./internal/output
go test ./cmd ./internal/client
```

## Filesystem fixtures

Use `t.TempDir` for private per-test data.
Use `filepath.Join` for all fixture paths.
Never read the host's actual dnsmasq lease file.
Never write a production hosts directory.
Set up a directory rather than sharing files across tests.
Check errors on fixture creation before calling the code under test.
Store files are readable text: assert their contents when relevant.
Also check that staged files are absent after completed writes.

## HTTP fixtures

Use `newHandlerFixture` for ordinary root-package tests.
It injects LeaseFile, Store, fixed token, and a counting Publisher.
Use `httptest.NewRecorder` for an in-process request.
The real ServeMux populates path values.
Do not call a handler directly with a manually fabricated path value.
Use `httptest.NewServer` for transport and command-package integration.
Close servers with deferred cleanup.
Supply Authorization headers explicitly in authenticated tests.

## Assertions

Use `t.Fatal` for failed setup or an unusable response.
Use `t.Errorf` for independent table rows.
Mark assertion and setup helpers with `t.Helper`.
Include response status and body in HTTP failure messages.
Decode JSON when testing fields rather than comparing property order.
Compare exact file lines when exercising hostsdir formatting.
Keep successful and unsuccessful state assertions distinct.

## Failure side effects

An invalid mutation must not create a file.
A missing-record update must not create a new record.
A rejected duplicate must not invoke Publisher.
A publisher failure is different: the durable mutation has already happened.
Assert the stored file as well as the publish error.
Do not make a rejected request look successful by relaxing an assertion.
Do not change baseline tests merely to hide a regression.

## Fakes and concurrency

A tiny Publisher fake substitutes only the reload boundary.
It can count calls and optionally return an error.
Never send SIGHUP to a real process in a handler test.
Use a mutex if a new fake is called concurrently.
Wait for goroutines with a WaitGroup or completion channel.
Use buffered result channels to avoid blocking workers before collection.
Race checks are useful for changes around Store locks.
Do not parallelize tests that change shared environment variables.

## Before handoff

Run the focused tests first.
Then run the whole module and vet.
Check formatting separately; tests do not enforce gofmt.
Review that fixtures do not depend on developer-specific absolute paths.
Check that no generated binary or cache is part of the source tree.
