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
    'ui/preferences-dialog.blp',
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

gnome.compile_resources('postcard',
  'postcard.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: [blueprints, appstream_file],
)

postcard_sources = [
  'postcard/__init__.py',
  'postcard/main.py',
  'postcard/application.py',
  'postcard/window.py',
  'postcard/conversation_row.py',
  'postcard/message_view.py',
  'postcard/account_dialog.py',
  'postcard/accounts_dialog.py',
  'postcard/composer_window.py',
  'postcard/preferences_dialog.py',
  'postcard/mail_sync.py',
]
python_installation.install_sources(postcard_sources, subdir: 'postcard')

postcard_core_sources = [
  'postcard/core/__init__.py',
  'postcard/core/secrets.py',
  'postcard/core/compose.py',
  'postcard/core/threader.py',
]

python_installation.install_sources(postcard_core_sources, subdir: 'postcard/core')

postcard_core_models_sources = [
  'postcard/core/models/__init__.py',
  'postcard/core/models/account.py',
  'postcard/core/models/folder.py',
  'postcard/core/models/email.py',
  'postcard/core/models/conversation.py',
  'postcard/core/models/attachment.py',
]
python_installation.install_sources(postcard_core_models_sources, subdir: 'postcard/core/models')

postcard_core_store_sources = [
  'postcard/core/store/__init__.py',
  'postcard/core/store/database.py',
]
python_installation.install_sources(postcard_core_store_sources, subdir: 'postcard/core/store')

postcard_core_net_sources = [
  'postcard/core/net/__init__.py',
  'postcard/core/net/errors.py',
  'postcard/core/net/imap_session.py',
  'postcard/core/net/smtp_session.py',
]
python_installation.install_sources(postcard_core_net_sources, subdir: 'postcard/core/net')

postcard_core_mime_sources = [
  'postcard/core/mime/__init__.py',
  'postcard/core/mime/message_parser.py',
]
python_installation.install_sources(postcard_core_mime_sources, subdir: 'postcard/core/mime')

conf = configuration_data()
conf.set('PYTHON', python_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'postcard.in',
  output: 'postcard',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)
