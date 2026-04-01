"""Frappe framework constants referenced by agent backstories and validation tools.

Maintained as a single source of truth. Changes here propagate to
agent behavior (via backstory references) and validation rules.
"""

# Valid Frappe field types (complete list as of v15+)
VALID_FIELD_TYPES = [
	"Autocomplete", "Attach", "Attach Image", "Barcode", "Button",
	"Check", "Code", "Color", "Column Break", "Currency", "Data",
	"Date", "Datetime", "Duration", "Dynamic Link", "Float",
	"Fold", "Geolocation", "Heading", "HTML", "HTML Editor", "Icon",
	"Image", "Int", "JSON", "Link", "Long Text", "Markdown Editor",
	"Password", "Percent", "Phone", "Read Only", "Rating",
	"Section Break", "Select", "Signature", "Small Text",
	"Tab Break", "Table", "Table MultiSelect", "Text",
	"Text Editor", "Time",
]

# Fieldnames reserved by Frappe (cannot be used as custom field names)
RESERVED_FIELDNAMES = [
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "parent", "parenttype", "parentfield",
	"doctype", "amended_from",
]

# Modules allowed in Server Script sandbox
ALLOWED_SCRIPT_MODULES = ["frappe", "json", "datetime", "math", "re"]

# Imports forbidden in Server Scripts
FORBIDDEN_SCRIPT_IMPORTS = [
	"os", "sys", "subprocess", "shutil", "importlib",
	"socket", "http", "urllib", "requests",
]

# Functions forbidden in Server Scripts
FORBIDDEN_SCRIPT_FUNCTIONS = [
	"eval", "exec", "compile", "__import__",
	"getattr", "setattr", "delattr",
]

# Naming rules supported by Frappe
NAMING_RULES = [
	"autoincrement",  # AUTO-00001
	"field:{fieldname}",  # Named by a field value
	"format:PREFIX-{####}",  # Custom format
	"Prompt",  # User enters name manually
	"hash",  # Random hash
	"naming_series:",  # From naming series field
]

# Valid DocType events for Server Scripts
SERVER_SCRIPT_EVENTS = [
	"Before Insert", "After Insert",
	"Before Validate", "Before Save", "After Save",
	"Before Submit", "After Submit",
	"Before Cancel", "After Cancel",
	"Before Delete", "After Delete",
	"Before Rename", "After Rename",
]

# Server Script types
SERVER_SCRIPT_TYPES = [
	"DocType Event",
	"Scheduler Event",
	"Permission Query",
	"API",
]
