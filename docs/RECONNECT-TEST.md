# Power-cycle recovery test — 1.0.1rc1

This test candidate adds a bounded fresh-service-discovery attempt when Home
Assistant connects but cannot find the required FFF0 service or FFF1/FFF2
characteristics. Incomplete cached services are a possible cause of the reported
error; the chair test below is needed to confirm recovery.

## Install the candidate

1. Keep a copy of the installed `custom_components/svago` folder for rollback.
2. Extract `svago-home-assistant-1.0.1rc1.zip` and replace the `svago` folder inside
   Home Assistant's `/config/custom_components/` with the included
   `custom_components/svago` folder.
3. Restart Home Assistant, leaving the Svago integration configured. Saved
   memories and timer choices are stored separately and are preserved.
4. Keep the Motion Mastery Bluetooth remote off and check that the chair reaches
   **Ready**. The diagnostics should report version `1.0.1rc1`.

## Check recovery

1. With the chair idle and Home Assistant showing **Ready**, switch off the chair
   using its normal power switch. Wait until Home Assistant shows **Disconnected**.
2. Switch the chair back on and allow up to two minutes for discovery and setup.
   Leave the Bluetooth remote off throughout the test.
3. Check that Home Assistant returns to **Ready** without reloading the integration
   and that no previous chair action resumes. Repeat once if successful.

If it remains disconnected, record the full setup error and download the Svago
diagnostics. The new `service_discovery` section reports whether cache recovery
was attempted and whether clearing succeeded. An error ending in **after fresh
service discovery** means the required services were also absent on the uncached
attempt; it does not establish that the remote caused the failure.

To roll back, restore the previous `custom_components/svago` folder and restart
Home Assistant. Keep the configured integration and saved data.
