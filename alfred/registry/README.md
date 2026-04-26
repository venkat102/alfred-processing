# Alfred registry

Two JSON-backed knowledge bases that the pipeline reads at runtime.
Both ship in-tree (no external service); both have a JSON-Schema
sibling that locks the shape so a typo in a rule body never silently
disables the rule.

```
registry/
├── intents/        # one file per intent the planner can pick
│   ├── _meta_schema.json
│   ├── create_doctype.json
│   ├── create_notification.json
│   └── ...
└── modules/        # one file per Frappe/ERPNext module
	├── _meta_schema.json
	├── _families/  # cross-module conventions
	├── accounts.json
	├── selling.json
	└── ...
```

Both are loaded by `alfred/registry/loader.py` (intents) and
`alfred/registry/module_loader.py` (modules) and cached for the
process lifetime — edits require a restart.

---

## intents/

One file per intent. Drives the planner's intent-classifier and the
per-intent Builder paths (the slot fields the user sees during the
plan-doc flow).

### Shape (see `intents/_meta_schema.json` for the authoritative
definition)

```json
{
  "intent": "create_notification",
  "display_name": "Create Notification",
  "doctype": "Notification",
  "fields": [
    {
      "key": "channel",
      "label": "Channel",
      "type": "select",
      "options": ["Email", "SMS", "System Notification"],
      "default": "Email",
      "rationale": "Email is the most common channel; safe default."
    }
  ]
}
```

### Field types

| `type` | What it captures | Required extras |
|---|---|---|
| `data` | Free-form short string | — |
| `text` | Free-form multi-line string | — |
| `int` | Integer | — |
| `check` | Boolean (0/1) | `default` |
| `select` | One of an enum | `options` (array of strings) |
| `link` | Reference to another DocType | `link_doctype` |
| `table` | Repeating child rows | `default` (array of row dicts) |

### Required vs optional fields

If a field is `"required": true`, the user MUST fill it before the
plan-doc commits. Otherwise `default` AND `rationale` are required by
the schema — `default` so we can ship a usable changeset without
asking, `rationale` so a future maintainer knows why this was the
right safe default.

---

## modules/

One file per Frappe/ERPNext module the agents may need to reason
about. Carries:

1. A long-form `backstory` string the module specialist agent
   reads to ground its assessments.
2. `conventions`: deterministic per-module defaults (extra roles,
   naming hints, required permission rows).
3. `validation_rules`: a deterministic rule set that fires AFTER
   the changeset is built but BEFORE it reaches the user. This is
   the substance of the post-builder review the deleted
   `*_builder.py` files used to do, now consolidated into
   `module_specialist.py:run_rule_validation`.
4. `detection_hints`: keywords the orchestrator uses to route a
   prompt to this module's specialist.

### Validation rule shape

```json
{
  "id": "accounts_sales_invoice_return_needs_return_against",
  "severity": "blocker",
  "when": {"doctype": "Sales Invoice", "data.is_return": 1},
  "message": "Sales Invoice with is_return=1 MUST link a return_against ...",
  "fix": "Set data.return_against to the original Sales Invoice or Delivery Note ..."
}
```

#### `id`

Snake-case, namespaced with the module name (`accounts_*`,
`selling_*`, etc.) so a grep across the registry shows where each
rule lives. Used as the source field on the resulting
`ValidationNote` so the UI can deep-link back to docs.

#### `severity`

One of three values — pin the spelling, the schema rejects anything
else:

| `severity` | Meaning at the UI | Effect on the deploy button |
|---|---|---|
| `advisory` | Informational note | Deploy enabled; pill is grey |
| `warning` | Concerning but not fatal | Deploy enabled; banner is yellow; button reads "Deploy Anyway" |
| `blocker` | Will fail at deploy time | Deploy DISABLED; user must rephrase or accept |

Secondary-module specialist notes have their `blocker` severity
capped to `warning` (`module_specialist.cap_secondary_severity`)
so a non-primary module can't gate the deploy off a hunch.

#### `when`

A flat dict of dotted-path keys to expected values. The rule fires
when EVERY entry matches the changeset item being checked. There is
NO Jinja, NO expression DSL, NO regex — just dict equality. Examples:

```jsonc
{"doctype": "Sales Invoice"}
// item.doctype == "Sales Invoice"

{"doctype": "Sales Invoice", "data.is_return": 1}
// item.doctype == "Sales Invoice" AND item.data.is_return == 1

{"doctype": "Custom Field", "data.fieldtype": "Link",
 "data.options": "Sales Invoice"}
// item is a Custom Field that links to Sales Invoice
```

If you need richer matching (regex, set membership, comparison), the
right move is to write the predicate in Python inside
`module_specialist._rule_applies` rather than building a DSL on top of
JSON. Keep this layer dumb on purpose.

#### `message`

The text shown to the user. Lead with the rule, then the source of
truth (file path + line number when possible — the existing rules
quote `sales_invoice.py:358` and similar so a user can verify in the
codebase). Imperative if the user can act on it; declarative if it's
informational.

#### `fix` (optional)

If there's a one-line corrective action the agent could apply,
write it here. The UI surfaces it under the message as a "Try this"
hint. Omit for advisory-only notes where there's nothing to fix.

### Detection hints

```json
"detection_hints": {
  "keywords": ["customer", "invoice", "AR aging"],
  "doctypes": ["Customer", "Sales Invoice"]
}
```

The orchestrator scores prompts against each module's hints; highest
score wins. Adding a keyword affects routing immediately on next
prompt (no agent retraining needed) — be conservative, every
keyword is a false-positive vector for some other module.

---

## Adding or editing a rule

1. Edit the relevant JSON file (`accounts.json`, etc.).
2. Validate against the schema: `jq` will catch JSON-syntax errors;
   the loader's load-time schema check will catch shape errors at
   the next process restart.
3. Add a rule to `tests/test_module_specialist.py` (or the
   per-module test) that exercises the new rule on a synthetic
   changeset. A rule without a regression test will rot.
4. Restart the processing app — the registry is cached for process
   lifetime.

## Why JSON, not YAML

YAML is friendlier to write but JSON is friendlier to lint, schema-
validate, and diff. The rule bodies are small enough that the
JSON quoting overhead is acceptable. If a future maintainer
proposes a switch to YAML, the bar is: schema validation must
still run at load time AND the diffs must stay small (no anchor
cleverness).
