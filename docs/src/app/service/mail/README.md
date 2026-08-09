# `app/service/mail`

Owns SMTP configuration use and outgoing registration/account messages. Durable
SMTP fields live in the singleton SQLite row; the password is encrypted and is
never emitted in status or audit output.
