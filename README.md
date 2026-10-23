# GURI website on Vercel

This folder is the complete website deployment. The Discord selfbot continues to
run separately on bot hosting. No bot tokens, customer records, license keys,
signing secrets or live environment files are included.

1. Upload the contents of this folder to a new GitHub repository.
2. Import that repository at https://vercel.com/new.
3. Use root directory `./`, framework Flask, and default build/install settings.
4. Add all variables from `.env.example` in Vercel project settings. Use the real
   SFTP values from your private `dashboard/.env`; do not upload that file. Set
   DASHBOARD_MODE to hosting. Choose a long private DASHBOARD_PASSWORD for admin.
5. Deploy. Check `/health`, the pricing page, and Login. Seller username: admin.
   Customers use their registered Discord username and separate dashboard password.
6. To add a domain, open Project Settings > Domains and add the domain. Apply the
   DNS records Vercel displays at your domain registrar. Wait for verification.
7. Redeploy after any environment-variable changes.

Plans: 7 days $2 USD; 30 days $5 USD; Lifetime $11 USD. Buy buttons currently open
plan details with checkout unavailable. A payment provider and verified payment
webhook still need to be connected before purchases or automatic key fulfillment.

The in-memory login attempt limiter is per running server instance. Before a
broad public launch, use shared rate limiting (or Vercel Firewall) across instances.
Keep bot hosting online; the website reads state and queues actions over SFTP.

For future code updates, rebuild this folder with `.backups/build_vercel.py`,
push the refreshed files to GitHub, and let Vercel redeploy.
