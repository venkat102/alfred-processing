"""Frappe framework knowledge - pointers for agents.

This module used to embed a ~165-line "Frappe quick reference" directly in
agent backstories. Successive refactors moved that content out of prompts
and into retrievable knowledge layers:

  1. Framework Knowledge Graph (`lookup_doctype`) - auto-extracted vanilla
     DocType metadata from the installed bench apps. Always current.
  2. Customization Pattern Library (`lookup_pattern`) - curated templates for
     common idioms (approval notification, validation script, audit log, etc.).
  3. Frappe Knowledge Base (`lookup_frappe_knowledge`) - platform rules,
     Frappe API reference, idioms (lifecycle, hooks, rename), and house
     style. Covers what agents repeatedly get wrong from training memory.

The pipeline's `inject_kb` phase auto-prepends the most relevant FKB entries
and site-state block to the Developer's task turn (hybrid keyword + semantic
retrieval on the enhanced prompt). Agents see the relevant context without
needing to call the retrieval tool themselves - but they can, for depth.

What lives here is a short set of pointers that tells the agent HOW to query.
"""

FRAPPE_REFERENCE = """
=== FRAPPE CUSTOMIZATION QUICK REFERENCE ===

You have THREE retrievable knowledge layers. In most cases the `inject_kb`
pipeline phase has already pulled the relevant entries into your task turn
(look for the "FRAPPE KB CONTEXT" and "SITE STATE FOR <DocType>" banners
above the USER REQUEST). Call these tools when you need to dig deeper:

1. INTENT + RECIPE (curated patterns)
   - `lookup_pattern(query, kind="search")` - find relevant curated pattern(s).
     Patterns include when_to_use, when_not_to_use, template, anti_patterns.
     Adapt the template to the user's actual target DocType.
   - `lookup_pattern(name, kind="name")` - retrieve a specific pattern by name.

2. SCHEMA VERIFICATION (framework KG + live site)
   - `lookup_doctype(name, layer="framework")` - vanilla field list for a DocType.
   - `lookup_doctype(name, layer="site")` - live site schema including customs.
   - `lookup_doctype(name, layer="both")` - merged view, flags customs separately.
   - Never recall field names from memory - always verify via lookup_doctype.

3. PLATFORM KNOWLEDGE (rules, APIs, idioms, style)
   - `lookup_frappe_knowledge(query, kind=<rule|api|idiom|style|empty>)` -
     hybrid keyword+semantic search over the Frappe Knowledge Base.
     rule  = sandbox / operational constraints (Server Script no imports, etc.)
     api   = Frappe function reference (frappe.db.get_value, frappe.utils.*, ...)
     idiom = how Frappe wants it done (hooks, lifecycle, rename, enqueue, ...)
     style = Alfred code-gen preferences (tabs, permission-check-first, _())
   - Most relevant entries are AUTO-INJECTED into your task - check the
     banner first before calling the tool.

4. PERMISSION + STATE CHECKS
   - `check_permission(doctype, action)` - verify rights before proposing.
   - `get_existing_customizations()` - site-wide summary of customizations.
   - `get_site_customization_detail(doctype)` - deep per-DocType recon
     (workflows, server scripts, custom fields). Auto-injected when the
     prompt names a DocType - check the SITE STATE banner first.
   - `has_active_workflow(doctype)` - one active workflow per DocType.
   - `get_user_context()` - roles and permitted modules.

5. VALIDATION + DRY-RUN
   - `dry_run_changeset(changes)` - savepoint-rollback validation. Call BEFORE
     presenting the final changeset. Catches missing mandatory fields, bad
     link targets, Python/JS syntax errors, Jinja template errors, forbidden
     `import` in Server Scripts.

STRUCTURAL FACTS (always true):
- Every changeset item has shape: {"op": "create", "doctype": "<TYPE>", "data": {...}}
- Every `data` object MUST include "doctype" matching the outer doctype
- Mandatory fields per doctype come from `lookup_doctype`, not from recall

CORE DOCTYPES TO NEVER RECREATE (use them, don't build new ones):
User, Role, Module Def, DocType, Custom Field, Property Setter,
Notification, Email Template, Auto Repeat, Assignment Rule,
Server Script, Client Script, Workflow, Workflow State, Workflow Action,
Print Format, Report, Dashboard, Web Page.

STAY IN THE USER'S DOMAIN. The target DocType must come from the user's actual
request. Read the user request, identify the exact DocType name (verbatim), and
use THAT name throughout. Never substitute a different DocType because an
example in a tool docstring or pattern template used one.
"""
