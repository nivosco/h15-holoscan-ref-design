project('hsb-synthetic-sender', 'cpp',
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

executable('hsb-synthetic-sender',
    'main.cpp',
    dependencies: hsb_emulator_deps,
    install: true)
