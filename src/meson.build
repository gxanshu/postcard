postbox_sources = [
  'main.vala',
  'application.vala',
  'window.vala',
]

postbox_deps = [
  config_dep,
  dependency('gtk4'),
  dependency('libadwaita-1', version: '>= 1.4'),
]

blueprints = custom_target('blueprints',
  input: files(
    'window.blp',
    'shortcuts-dialog.blp',
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

postbox_sources += gnome.compile_resources('postbox-resources',
  'postbox.gresource.xml',
  c_name: 'postbox',
  dependencies: blueprints,
)

executable('postbox', postbox_sources,
  dependencies: postbox_deps,
  include_directories: config_inc,
       install: true,
)
