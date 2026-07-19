python = import('python')
python_installation = python.find_installation('python3')

pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

blueprints = custom_target('blueprints',
  input: files(
    'ui/main-window.blp',
    'ui/shortcuts-dialog.blp',
    'ui/account-dialog.blp',
    'ui/accounts-dialog.blp',
    'ui/composer-window.blp',
  ),
  output: '.',
  command: [
    find_program('blueprint-compiler'),
    'batch-compile',
    '@OUTPUT@',
    '@CURRENT_SOURCE_DIR@',
    '@INPUT@',
  ],
)

gnome.compile_resources('postbox',
  'postbox.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

postbox_sources = [
  'postbox/__init__.py',
  'postbox/main.py',
  'postbox/application.py',
  'postbox/window.py',
  'postbox/conversation_row.py',
  'postbox/message_view.py',
  'postbox/account_dialog.py',
  'postbox/accounts_dialog.py',
  'postbox/composer_window.py',
  'postbox/mail_sync.py',
  'postbox/mail_send.py',
]
python_installation.install_sources(postbox_sources, subdir: 'postbox')

postbox_core_sources = [
  'postbox/core/__init__.py',
  'postbox/core/secrets.py',
  'postbox/core/compose.py',
  'postbox/core/threader.py',
]

python_installation.install_sources(postbox_core_sources, subdir: 'postbox/core')

postbox_core_models_sources = [
  'postbox/core/models/__init__.py',
  'postbox/core/models/account.py',
  'postbox/core/models/folder.py',
  'postbox/core/models/email.py',
  'postbox/core/models/conversation.py',
  'postbox/core/models/attachment.py',
]
python_installation.install_sources(postbox_core_models_sources, subdir: 'postbox/core/models')

postbox_core_store_sources = [
  'postbox/core/store/__init__.py',
  'postbox/core/store/database.py',
]
python_installation.install_sources(postbox_core_store_sources, subdir: 'postbox/core/store')

postbox_core_net_sources = [
  'postbox/core/net/__init__.py',
  'postbox/core/net/imap_session.py',
  'postbox/core/net/smtp_session.py',
]
python_installation.install_sources(postbox_core_net_sources, subdir: 'postbox/core/net')

postbox_core_mime_sources = [
  'postbox/core/mime/__init__.py',
  'postbox/core/mime/message_parser.py',
]
python_installation.install_sources(postbox_core_mime_sources, subdir: 'postbox/core/mime')

conf = configuration_data()
conf.set('PYTHON', python_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'postbox.in',
  output: 'postbox',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)
