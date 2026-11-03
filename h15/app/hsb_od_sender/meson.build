project('h15-hsb-sender', 'cpp',
    version: '0.1.0',
    license: 'MIT',
    default_options: ['cpp_std=c++20', 'warning_level=2', 'buildtype=release'])

cpp = meson.get_compiler('cpp')

# The HSB Emulator installs static libraries without a pkg-config file; link order matters.
hsb_emulator_deps = [
    cpp.find_library('emulationroce'),
    cpp.find_library('emulationcoe'),
    cpp.find_library('emulation_host'),
    cpp.find_library('emulation_common'),
    dependency('zlib'),
    dependency('threads'),
]

hailo_deps = [
    dependency('hailo-medialib'),
    dependency('hailo-analytics'),
    dependency('hailo-postprocess-tools', include_type: 'system'),
]

executable('h15-hsb-sender',
    'main.cpp',
    'hsb_sink_stage.cpp',
    include_directories: include_directories('../../common/include'),
    dependencies: hsb_emulator_deps + hailo_deps,
    install: true)
