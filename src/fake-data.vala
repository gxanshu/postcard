/**
* temporary hardcoded sample data so we can build and feel the three-pane UI
* before any networking or database exist, these are plain data objects
* and they get deleted in phase 3
*/

namespace Postbox {
    public class FakeEmail: GLib.Object {
        public string sender { get; set; }
        public string subject { get; set; }
        public string preview { get; set; }
        public string date { get; set; }
        public bool unread { get; set; }

        public FakeEmail (string sender, string subject, string preview, string date, bool unread) {
            Object(
                sender: sender,
                subject: subject,
                preview: preview,
                date: date,
                unread: unread
            );
        }
    }

    public class FakeFolder: GLib.Object {
        public string name { get; set; }
        public string icon_name { get; set; }
        public GLib.ListStore emails { get; set; }

        public FakeFolder(string name, string icon_name) {
            Object (name: name, icon_name: icon_name);
            this.emails = new GLib.ListStore(typeof(FakeEmail));
        }

        public void add(string sender, string subject, string preview, string date, bool unread) {
            this.emails.append(
                new FakeEmail(sender, subject, preview, date, unread)
            );
        }
    }

    namespace FakeData {
        public GLib.ListStore folders() {
            var store = new GLib.ListStore(typeof(FakeFolder));

            var inbox = new FakeFolder(_("inbox"), "mail-inbox-symbolic");
            inbox.add ("GNOME Foundation", "Welcome to GNOME 48",
                       "Thanks for joining the community — here's what shipped this cycle and how to get involved.",
                       "09:42", true);
            inbox.add ("Migadu Support", "Your mailbox is ready",
                       "Your new mailbox anshu@postbox.dev is provisioned. IMAP and SMTP settings are below.",
                       "08:15", true);
            inbox.add ("Ada Lovelace", "Re: Lunch on Thursday?",
                       "Thursday works great for me. Let's meet at the usual place around noon.",
                       "Yesterday", false);
            inbox.add ("Vala Weekly", "Async/await, explained simply",
                       "This week: a gentle walk through yield, plus a reader question about GListModel.",
                       "Mon", false);
            inbox.add ("Grace Hopper", "Debugging tips",
                       "Attached are the notes from the talk. The bit about reading the reference, not copying it, applies here too.",
                       "Sun", false);
            store.append (inbox);

            var starred = new FakeFolder (_("Starred"), "starred-symbolic");
            starred.add ("Ada Lovelace", "Re: Lunch on Thursday?",
                         "Thursday works great for me. Let's meet at the usual place around noon.",
                         "Yesterday", false);
            store.append (starred);

            var sent = new FakeFolder (_("Sent"), "mail-send-symbolic");
            sent.add ("Me", "Re: Your mailbox is ready",
                      "Thanks! Got it working. Now building an email client to actually read it in.",
                      "08:31", false);
            store.append (sent);

            var drafts = new FakeFolder (_("Drafts"), "document-edit-symbolic");
            drafts.add ("Me", "(no subject)",
                        "Hey, just wanted to say —",
                        "10:02", false);
            store.append (drafts);

            var trash = new FakeFolder (_("Trash"), "user-trash-symbolic");
            store.append (trash);

            return store;
        }
    }
}
