# Telegram Share-to-Unlock Bot

Een Telegram bot die werkt met een *share-to-unlock* systeem:  
Deel de groep met 2 nieuwe leden → unlock toegang tot een privé groep.

## 🚀 Deploy (Render)

1. Fork deze repo en zet je eigen GitHub repository op.  
2. Maak een nieuwe **Web Service** in Render en selecteer je repo.  
3. Zorg dat Render de `Dockerfile` bouwt.  
4. Zet environment variables in:
   - `BOT_TOKEN` = jouw bot token via @BotFather  
   - `MAIN_CHAT_ID` = het ID van je hoofdgroep (bv. `-1001234567890`)  
   - `PRIVATE_GROUP_LINK` = de link naar je privé groep  
   - `WEBHOOK_SECRET` = random string, b.v. `mysecret123`  

5. Deploy en stel webhook in:
