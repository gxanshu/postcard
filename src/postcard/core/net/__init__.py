# Constants shared by the IMAP and SMTP sessions. They live here rather than in
# either session module so neither has to import the other.

# Socket timeout for both protocols. Generous on purpose: a first sync over a
# slow link can take a while, and neither imaplib nor smtplib reports partial
# progress, so a tight timeout looks like a connection failure.
NET_TIMEOUT_SECONDS = 30
