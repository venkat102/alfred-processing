"""Processing-app-side knowledge retrieval.

Currently hosts the FKB (Frappe Knowledge Base) client and hybrid retrieval
stack. The source-of-truth YAMLs live in alfred_client/data/frappe_kb/; this
package loads them directly (no MCP round-trip) and adds a semantic layer
on top that wouldn't fit in the Frappe web worker (sentence-transformers
is ~1GB of deps and the CLAUDE.md policy is "never touch bench venv").
"""
