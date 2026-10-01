# Live Query Log controls

Click **Start Live View** to enable live polling. The blue start button becomes an outlined **Pause Live View** action; **Clear view** appears beside it as a secondary action. Clearing affects only the displayed browser view and keeps AdGuard Home's server Query Log/history, as explained by the button's tooltip and screen-reader description.

While reading older rows, new arrivals wait behind **17 new queries — Show newest**. Activating it reveals the queued queries and returns to the newest entries. The screenshots use sample blocked queries for `google.com`; the browser fixtures also use `microsoft.com` when validating filter changes.

These are visible-viewport captures of the production frontend from the existing browser harness, with deterministic API fixtures. Desktop is 1440 × 900; mobile is 390 × 844. The harness also checks the same states at 320 × 740, including action names, keyboard focus/activation, spacing, preference persistence, and queued arrivals through client block/unblock.

## Desktop, inactive

![Desktop Query Log with Start Live View](query-log-desktop-inactive.png)

## Desktop, Live

![Desktop Query Log with Pause Live View and secondary Clear view](query-log-desktop-live.png)

## Mobile, inactive

<img src="query-log-mobile-390-inactive.png" alt="Mobile Query Log with Start Live View" width="390">

## Mobile, Live

<img src="query-log-mobile-390-live.png" alt="Mobile Query Log with Pause Live View and secondary Clear view on one row" width="390">

## Mobile, queued queries

<img src="query-log-mobile-390-pending.png" alt="Mobile Query Log scrolled to older rows with 17 new queries — Show newest" width="390">
