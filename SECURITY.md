# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button on this repository's Security tab (private vulnerability reporting). Do not open a public issue for a security problem. You can expect an acknowledgement within a week; a fix or a mitigation is released as soon as it is ready, and the report is credited unless you ask otherwise.

## Supported versions

Only the latest release receives fixes while the project is in alpha (0.x).

## What we consider a vulnerability

- Any way for a tool call to **write** upstream (orders, positions, account settings, watchlists) or to enable such a tool.
- Any way for `results_query` (or any other tool) to read or write a **file** other than the session's own results and the configured export folder, reach the network, read another session's results, load an extension or change DuckDB settings.
- A **filesystem path, secret or key** disclosed in a response, an error or a log line sent to the model.
- Bypassing the **HTTP bearer token** or the loopback-only binding, or the Host/Origin checks.
- Output of a tool reaching the model **without the trust envelope**, or third-party text without the untrusted-text notice.
- A plugin that is not listed in `plugins.enabled` being imported.

## Design notes

- Tools are read-only by construction: the write operations of the upstream APIs are not implemented, and tool annotations are forced read-only.
- Model-written SQL passes a parser-based guard (one SELECT, no functions that read files or settings, tables only by this session's result ids) and then runs in a fresh in-memory DuckDB with external access disabled and its configuration locked, with a forced row limit, a timeout and a memory cap. DuckDB error texts are scrubbed of paths.
- Results are stored as owner-only files in the user's cache directory, expire after 24 hours and are capped in size.
- marketlens-mcp sends no telemetry; the FastMCP banner's update check is disabled.
- Plugins run in-process: installing a plugin is trusting its code.
