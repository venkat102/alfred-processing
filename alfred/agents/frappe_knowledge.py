"""Frappe framework knowledge - pointers for agents.

This module used to embed a ~165-line "Frappe quick reference" directly in
agent backstories. Phase 1 of the architecture refactor moved that content to
two queryable layers exposed via MCP tools:

  1. Framework Knowledge Graph (`lookup_doctype`) - auto-extracted vanilla
     DocType metadata from the installed bench apps. Always current.
  2. Customization Pattern Library (`lookup_pattern`) - curated templates for
     common idioms (approval notification, validation script, audit log, etc.).

Agents should query those tools on demand instead of reading a static reference.
What lives here is a short set of pointers that tells the agent HOW to query.
"""

FRAPPE_REFERENCE = """
=== FRAPPE CUSTOMIZATION QUICK REFERENCE ===

You have four layers of tools available. Use them in this order for any request:

1. INTENT DISCOVERY (pattern library)
   - `lookup_pattern(query, kind="search")` - find relevant curated pattern(s)
     for the user's request. Patterns include when_to_use, when_not_to_use,
     template, and anti_patterns sections. Adapt the template to the user's
     actual target DocType.
   - `lookup_pattern(name, kind="name")` - retrieve a specific pattern once
     you know its name.

2. SCHEMA VERIFICATION (framework KG + live site)
   - `lookup_doctype(name, layer="framework")` - vanilla field list for a
     DocType (what it ships with out of the box). Use this to check that a
     DocType exists and what fields it provides.
   - `lookup_doctype(name, layer="site")` - live site schema including any
     custom fields already installed.
   - `lookup_doctype(name, layer="both")` - merged view that flags custom
     fields separately from framework fields.
   - Never recall field names from memory - always verify via lookup_doctype.

3. PERMISSION + STATE CHECKS
   - `check_permission(doctype, action)` - verify the user has the rights
     before proposing a change.
   - `get_existing_customizations()` - list already-installed customizations
     so you don't duplicate one that exists.
   - `has_active_workflow(doctype)` - check before adding a workflow, since
     Frappe allows only one active workflow per DocType.
   - `get_user_context()` - roles and permitted modules for the current user.

4. VALIDATION + DRY-RUN
   - `dry_run_changeset(changes)` - savepoint-rollback validation against the
     live site. Call BEFORE presenting the final changeset. It catches missing
     mandatory fields, bad link targets, Python/JS syntax errors, and Jinja
     template errors.

STRUCTURAL FACTS (always true, not opinions):
- Every changeset item has shape: {"op": "create", "doctype": "<TYPE>", "data": {...}}
- Every `data` object MUST include "doctype" matching the outer doctype
- Mandatory fields per doctype come from `lookup_doctype`, not from recall

CORE DOCTYPES TO NEVER RECREATE (use them, don't build new ones):
User, Role, Module Def, DocType, Custom Field, Property Setter,
Notification, Email Template, Auto Repeat, Assignment Rule,
Server Script, Client Script, Workflow, Workflow State, Workflow Action,
Print Format, Report, Dashboard, Web Page.

PREFER MINIMAL CHANGES:
- Email / alert requirement -> built-in Notification DocType (NOT a Server Script)
- New field on an existing DocType -> Custom Field (NOT a new DocType)
- Multi-state approval -> Workflow (check has_active_workflow first)
- Only create a new DocType when the user genuinely needs a new ENTITY.

STAY IN THE USER'S DOMAIN. The target DocType must come from the user's actual
request. Read the user request, identify the exact DocType name (verbatim), and
use THAT name throughout your plan and output. Never silently switch to a
different DocType because an example in a tool docstring or pattern library
used one.
"""
