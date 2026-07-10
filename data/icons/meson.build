application_id = 'in.gxanshu.postbox'

# App icon (rasterized from the project logo) installed at the standard
# hicolor sizes. The shell/desktop picks the best size by name automatically.
icon_sizes = ['48', '64', '128', '256', '512']
foreach size : icon_sizes
  size_dir = 'hicolor' / (size + 'x' + size) / 'apps'
  install_data(
    size_dir / ('@0@.png').format(application_id),
    install_dir: get_option('datadir') / 'icons' / size_dir
  )
endforeach

symbolic_dir = 'hicolor' / 'symbolic' / 'apps'
install_data(
  symbolic_dir / ('@0@-symbolic.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / symbolic_dir
)
