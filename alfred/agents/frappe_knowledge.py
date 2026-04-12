"""Frappe framework knowledge base - injected into agent prompts for context.

This is a condensed reference of Frappe patterns, conventions, and APIs that
agents need to generate correct customizations. It supplements the LLM's
training data with precise, up-to-date Frappe specifics.
"""

FRAPPE_REFERENCE = """
=== FRAPPE FRAMEWORK QUICK REFERENCE (general guidance) ===

IMPORTANT: This reference describes the *typical* Frappe/ERPNext/HRMS structure. It is NOT
authoritative for any specific site. Every site can add custom fields, rename fields, install
app versions with different schemas, or remove built-in fields.

**Before designing or generating any solution, you MUST verify DocType details against the
live site by calling `get_doctype_schema(doctype)`.** The MCP tool returns the real field
list for this specific site, including any custom fields that aren't in this reference.

Treat the reference below as a starting point for knowing what EXISTS in Frappe conceptually.
Treat `get_doctype_schema` as the source of truth for field names, types, and options.

## Core DocTypes (already exist - NEVER recreate these)
- User, Role, Module Def, DocType, Custom Field, Property Setter
- Notification, Email Template, Auto Repeat, Assignment Rule
- Server Script, Client Script, Workflow, Workflow State, Workflow Action
- Print Format, Report, Dashboard, Web Page

## ERPNext DocTypes (already exist)
- Customer, Supplier, Item, Item Group, Warehouse, BOM
- Sales Order, Sales Invoice, Delivery Note, Purchase Order, Purchase Invoice, Purchase Receipt
- Stock Entry, Material Request, Stock Reconciliation
- Journal Entry, Payment Entry, GL Entry
- Project, Task, Timesheet

## HRMS DocTypes (already exist)
- Employee, Department, Designation, Branch, Employment Type
- Expense Claim (fields: employee, expense_approver, approval_status, total_claimed_amount, company)
- Leave Application (fields: employee, leave_approver, status, leave_type, from_date, to_date)
- Attendance, Salary Slip, Payroll Entry, Shift Assignment
- Employee Checkin, Leave Type, Holiday List

## Notification DocType (built-in email/alert system)
Use this instead of Server Scripts when you just need to send emails or alerts.
Fields:
  - subject: Email subject (supports Jinja: {{ doc.name }})
  - document_type: Link to DocType (e.g., "Expense Claim")
  - event: New | Save | Submit | Cancel | Days After | Days Before | Value Change | Method | Custom
  - channel: Email | Slack | System Notification
  - recipients: Table of Notification Recipient (receiver_by_document_field, receiver_by_role, cc, bcc)
  - condition: Python expression (e.g., doc.approval_status == "Draft")
  - message: HTML/Jinja template for email body
  - send_to_all_assignees: Check
  - attach_print: Check (attach print format)

Example - Email expense approver on new Expense Claim:
  document_type: Expense Claim
  event: New
  channel: Email
  recipients: receiver_by_document_field = expense_approver
  subject: New Expense Claim {{ doc.name }} from {{ doc.employee_name }}
  message: <p>A new expense claim has been submitted for your approval.</p>

## DocType Field Types
Data, Text, Small Text, Long Text, Text Editor, HTML Editor, Code, Password
Int, Float, Currency, Percent, Rating, Duration
Date, Datetime, Time
Link (options=DocType name), Dynamic Link, Table (child table), Table MultiSelect
Select (options=newline-separated values), Check (0/1), Color, Geolocation
Attach, Attach Image, Signature, Barcode, Read Only
Section Break, Column Break, Tab Break, HTML, Heading, Image, Button

## DocType Naming Rules
autoincrement, Prompt, field:fieldname, format_value (with autoname pattern)
naming_rule options: "Set by user" | "Autoincrement" | "By fieldname" | "By Naming Series" | "Expression" | "Random"

## Server Script Events
Before Insert, After Insert, Before Save, After Save, Before Submit, After Submit,
Before Cancel, After Cancel, Before Delete, After Delete

Server Script template:
  doc_event = "After Insert"  # or other event
  reference_doctype = "Expense Claim"
  script = '''
  # Always check permissions first
  if not frappe.has_permission("Expense Claim", "read", doc.name):
      frappe.throw("Not permitted")
  # Your logic here
  frappe.sendmail(
      recipients=[doc.expense_approver],
      subject=f"New Expense Claim {doc.name}",
      message=f"Please review expense claim {doc.name}"
  )
  '''

## Client Script Events
Form: Refresh, Validate, Before Save, After Save, Before Submit, Before Cancel,
      Timeline Refresh, Before Load
Field: Change (trigger on specific field change)
List: Refresh

Client Script template:
  dt = "Expense Claim"
  view = "Form"
  script = '''
  frappe.ui.form.on('Expense Claim', {
      refresh(frm) {
          // Your logic here
      },
      expense_type(frm) {
          // Triggered when expense_type field changes
      }
  });
  '''

## Workflow Definition
- document_type: The DocType this workflow applies to
- is_active: 1
- states: [{ state: "Draft", doc_status: "0", allow_edit: "Employee" }, ...]
- transitions: [{ state: "Draft", action: "Submit", next_state: "Pending Approval", allowed: "Employee" }, ...]
- Only ONE active workflow per DocType is allowed

## Key Frappe Python APIs
frappe.get_doc(doctype, name) - Fetch a document
frappe.new_doc(doctype) - Create new document instance
frappe.db.get_value(doctype, filters, fieldname) - Read a field value
frappe.db.set_value(doctype, name, fieldname, value) - Update a field
frappe.db.exists(doctype, name) - Check if document exists
frappe.has_permission(doctype, ptype, doc) - Check permission
frappe.throw(msg) - Raise error and stop execution
frappe.msgprint(msg) - Show message to user
frappe.sendmail(recipients, subject, message) - Send email
frappe.publish_realtime(event, data, user) - Push real-time event

## Key Frappe JS APIs
frappe.call({ method, args, callback }) - Call server method
cur_frm.set_value(fieldname, value) - Set field value
cur_frm.toggle_display(fieldname, show) - Show/hide field
cur_frm.set_query(fieldname, filters) - Filter Link field options
frappe.ui.form.on(doctype, { event: handler }) - Form event handler
frappe.show_alert({ message, indicator }) - Show alert

## DECISION GUIDE: What to create?
- Need email/SMS notification on doc event? → Use Notification DocType (NO code needed)
- Need to add a field to existing DocType? → Custom Field (NO new DocType)
- Need custom validation logic? → Server Script (Before Save event)
- Need UI behavior (show/hide fields, filters)? → Client Script
- Need approval flow? → Workflow
- Need a completely new business entity? → New DocType (only as last resort)
- Need to change field label/default/hidden? → Property Setter

=== END REFERENCE ===
"""
