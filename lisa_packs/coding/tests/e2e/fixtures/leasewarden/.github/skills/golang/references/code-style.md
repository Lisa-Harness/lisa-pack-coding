# Code style and boundaries

## Package ownership

The root package is an executable, not an importable service library.
Keep HTTP domain logic close to its existing handlers.
`cmd` owns the client command surface.
`internal/client` owns HTTP transport details.
`internal/output` owns presentation of decoded JSON.
`internal/macs` owns the stable device key.
Do not create a generic utilities package to share one helper.
Do not move server types into the transport package for convenience.

## Names

Use short lowercase package names without underscores.
Export only functions needed across a package boundary.
Use `MAC`, `IP`, `HTTP`, `JSON`, `URL`, and `PID` consistently.
Prefer concrete names such as `LeaseFile` and `Reservation`.
A receiver should be short and locally unambiguous.
Use `err` for the immediate operation's failure.
Use longer names when two errors must be retained together.
Sentinel errors are unexported in the root package.
JSON tags use lowercase snake case.

## Formatting

Run gofmt rather than manually aligning Go code.
Go 1.23 supports `any` and integer range loops.
Do not introduce older-runtime workarounds.
Do not suppress vet warnings to make validation pass.
Prefer one operation per statement on the normal path.
Keep a narrow diff when editing a file.
Avoid reorganizing unrelated imports or declarations by hand.

## Control flow

Validate at the start of an operation.
Return when validation fails.
Keep the write path linear and easy to audit.
Avoid an `else` block after an unconditional return.
Errors should be returned rather than panicked.
HTTP handlers map errors at the boundary.
A helper that emits a response signals whether the caller may continue.
Never emit a second response after an error helper fails.

Example error propagation:

```go
records, err := file.All()
if err != nil {
    return nil, fmt.Errorf("load records: %w", err)
}
return records, nil
```

## Errors

Use `%w` when preserving a cause matters to callers.
Use `errors.Is` for Store's missing and duplicate sentinels.
Use `errors.As` when inspecting a bounded-body failure.
Do not compare `err.Error()` strings for control flow.
Report operator errors on stderr at the entrypoint.
Do not include the token in error strings.
HTTP clients expose both status and machine-readable error code.
Do not flatten an HTTP failure into an empty successful result.

## Data structures

Allocate empty result slices when JSON should contain `[]`.
Do not let map iteration decide public response order.
Normalize keys before using them for comparisons or paths.
Avoid pointers for immutable row values unless absence is meaningful.
Use pointer receivers for structures containing a mutex.
Never copy a Store after first use.
Return fresh snapshots rather than an internal mutable cache.

## Concurrency

Store's mutex guards individual file operations.
The API's write mutex guards mutation and publisher ordering.
Read handlers do not need the API write lock.
Keep locks visibly paired with deferred unlocks.
Do not take the Store lock and then call a public Store method.
Its private read helper exists to avoid recursive locking.
No lock coordinates separate operating-system processes.
A single service process must own a host directory.
Goroutines need an owner and an observable completion condition.

## Comments and interfaces

Comment an exported symbol's contract, not every implementation step.
Explain non-obvious ordering, especially durable write versus reload.
The Publisher interface is small because tests replace the OS boundary.
Do not add a large repository interface over the concrete Store.
Do not invent dependency containers.
Constructor injection by hand is sufficient here.

## Review checklist

- Did the change stay within the package's ownership?
- Are errors returned without hiding the original failure?
- Are canonical strings used for all identities?
- Are lock lifetimes explicit and bounded?
- Does a new goroutine stop when its owner stops?
- Are public response collections deterministic?
- Are comments describing genuine constraints?
- Does gofmt print no pending changes?
