# Optional automation examples

The integration normally connects to a chair that stays powered on. Commands
that require the chair controller to be On activate it before proceeding.

For an automation, wait for **Connection status → Ready** before issuing a chair
command. Use a wait for a template, so an already-ready chair proceeds at once;
a wait for a state-change trigger alone can miss that case.

## Wait for connection readiness

Copy [wait_until_ready.yaml](../examples/wait_until_ready.yaml) into a new script
using **Settings → Automations & scenes → Scripts → Add script → Edit in YAML**.
Save it, then run it with your chair's **Connection status** sensor. The field
uses an entity selector, so no example entity ID needs to match your system.
Supply that field when calling the script from an automation as well; it is
checked at runtime.

The script waits up to one minute and sends no chair commands. An overlapping
invocation replaces the older wait. Afterward, your automation can perform its
chosen action once its own readiness condition passes.

If an existing automation or startup script references the removed Turn on
button, replace that step with this wait and the desired chair action, guarded
by the Ready condition below.

Call the script directly from an automation to wait for it to finish. Using
`script.turn_on` starts it in the background and does not wait for readiness.
The timeout ends this script only. Before subsequent chair actions in the calling
automation, add a condition that **Connection status** is still **Ready**; do not
treat the script returning as proof that its wait succeeded. Recheck readiness
after additional delays too.

## Continuous and memory automations

Choose Continuous, then explicitly turn Heat on or select a massage mode.
Alternatively, recall a memory saved with that function On and Continuous.
The corresponding **continuous status** sensor shows whether HA is managing renewal.
A remembered preference after a restart does not itself start the function.

Keep **Set memory** off in ordinary recall automations. When it is on, a Memory
button saves the current settings instead of recalling them. Saving is intended
as an explicit setup action.

Official references:
- [Scripts](https://www.home-assistant.io/integrations/script/)
- [Automation building blocks](https://www.home-assistant.io/building-blocks/)
