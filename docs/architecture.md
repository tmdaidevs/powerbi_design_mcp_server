# Architecture Overview

## Layer 1 - MCP Server Layer
`src/server/mcp_server.py` registers MCP tools with FastMCP and exposes LLM-friendly tool contracts.

## Layer 2 - Fabric API Client Layer
`src/fabric_client/client.py` handles REST calls, token headers, retries, throttling, and long-running operation polling.

## Layer 3 - Report Definition Parser Layer
`src/parser/definition_parser.py` normalizes definition parts into typed report/page/visual/bookmark/resource models.

## Layer 4 - Transformation Engine
`src/transformations/style_engine.py` applies structured style guide rules and emits deterministic transformation plans.

## Layer 5 - Validation Layer
`src/validation/validator.py` checks compatibility, referential integrity, and unsupported/risky artifacts.

## Layer 6 - Diff/Preview Layer
`src/diffing/diff_engine.py` computes before/after differences at part and field level for dry-run and approval workflows.
