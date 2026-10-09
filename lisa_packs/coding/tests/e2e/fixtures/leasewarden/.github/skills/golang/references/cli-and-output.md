# CLI, transport, and output

## Dispatcher

`main.go` recognizes `server` and `version` before client dispatch.
All other commands go through `cmd.Run`.
Keep one executable rather than introducing a second main package.
The command package receives context, stdout, and stderr explicitly.
It returns errors rather than calling `os.Exit`.
This keeps commands testable without helper subprocesses.

## Flag placement

Client flags precede the resource command.
Server flags follow the `server` command.
The standard flag parser stops at positional arguments.
Do not imply that GNU-style interspersed flags are supported.
Help is implemented before any HTTP client is constructed.
No token or server is needed to see usage.

Example client calls:

```sh
leasewarden --output json leases
leasewarden --server http://lab-router:8068 reservations list
leasewarden --timeout 2s addresses aabbccddee01
```

## Configuration precedence

Explicit flags override environment-derived defaults.
Environment values override built-in defaults when nonempty.
`LEASEWARDEN_SERVER` supplies the base URL.
`LEASEWARDEN_OUTPUT` supplies table or JSON mode.
`LEASEWARDEN_TOKEN` supplies the bearer token.
There is deliberately no token command-line flag.
The timeout defaults to five seconds.
A nonpositive timeout is a command validation error.
Use a fresh FlagSet per Run invocation.
Do not reuse package-global flag state between tests.

## Request mapping

Lease and client commands issue GET requests.
Address and device queries escape the path argument.
Reservation list and get do not supply a JSON body.
Add issues POST with MAC, IP, and optional hostname.
Update issues PUT to the MAC path.
Delete issues DELETE to the MAC path.
Validate positional argument counts before constructing the client.
Unknown commands should fail locally rather than contact the server.
Do not add shell-outs to curl for ordinary client requests.

## Transport ownership

`internal/client` is the only client-side HTTP implementation.
Use `http.NewRequestWithContext` for cancellation.
The Client copies its configured URL and token.
An HTTP timeout bounds stalled requests.
Response bodies are closed on every path.
Limit response reads before decoding.
No automatic retry is permitted, even for reads.
Redirects are returned as failures rather than followed.
This avoids forwarding credentials or repeating mutations elsewhere.

## URL rules

The base URL must be absolute HTTP or HTTPS.
A host is required.
Credentials in the URL are forbidden.
Query strings and fragments are forbidden.
The base may have a deployment path prefix.
Trailing slash removal prevents duplicate separators.
Resource arguments are escaped as path components.
Do not concatenate raw user values into query syntax.

## Response and error handling

Success statuses are the full 2xx range.
204 has no response document to decode.
Use `APIError` to preserve a failing status and error code.
When a failure body is not JSON, use the HTTP status text.
Do not print arbitrary HTML error bodies to the terminal.
A failed JSON decode after success is still an error.
Do not replace it with an empty list.
The caller chooses how to present a successful decoded document.

## JSON output

Use the standard encoder with two-space indentation.
Preserve object, array, and null shapes.
Keep a final newline for shell pipelines.
Do not add colors or table headings to JSON mode.
Do not normalize identifiers again in presentation.
Normalization belongs at the domain boundary.
A successful DELETE produces null in JSON mode.

## Table output

Tables accept a decoded object or array of objects.
Use the union of all row keys for columns.
Preferred domain columns appear first.
Additional columns sort alphabetically.
Missing fields produce blank cells.
Array cells use comma-separated element text.
Control characters are replaced with spaces.
Empty arrays print `(empty)`.
A nil value prints no table content.
Unsupported shapes and formats are explicit errors.
Writer failures must propagate to the command caller.

## Changes to verify

Test flag/environment precedence without contacting a real host.
Test request method, route, and token using an HTTP test server.
Test decoding and error codes independently from rendering.
Update CLI examples when positional arguments change.
Run both command and transport tests after a request change.
