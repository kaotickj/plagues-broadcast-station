# Third-party software notices

Plague's Broadcast Station's native package includes Python, Qt for Python (PySide6/Shiboken/Qt), NumPy and its numerical libraries, the PyInstaller bootloader, and FFmpeg/ffprobe with their dependent media libraries. The optional AI model environment and weights are not part of this GUI package.

## FFmpeg and ffprobe

The package includes the FFmpeg tools found on the native build host. The exact version, build configuration and license output are recorded in `licenses/FFmpeg-build-info.txt` inside the bundle's `_internal` folder. GPL and LGPL license texts are included under `licenses/`; the applicable FFmpeg configuration is identified by the build information. FFmpeg is a separate executable invoked by this application.

FFmpeg project source and licensing information: https://ffmpeg.org/ and https://ffmpeg.org/legal.html . Windows CI installs the FFmpeg package from Chocolatey; the package page identifies its binary upstream: https://community.chocolatey.org/packages/ffmpeg . For Debian-derived builds, the installed package copyright notices and package/source versions are included when available; corresponding package source is published by Debian at https://sources.debian.org/src/ffmpeg/ and https://deb.debian.org/debian/pool/main/f/ffmpeg/ . Use the recorded exact version and upstream build configuration when obtaining corresponding source.

Redistribution of a GPL-configured FFmpeg build requires satisfying that build's GPL source-distribution requirements; this notice and links are not a substitute for providing required corresponding source or a valid source offer with a public redistribution. Keep the full applicable notices and source information with each redistributed build. No FFmpeg program or library has been modified by this application's source.

## Qt for Python and Qt

The PySide6/Shiboken wheels identify their available licenses in the bundled distribution metadata. Qt for Python is available under LGPLv3/GPL or a commercial Qt license; this package uses the open-source distribution. Qt modules and bundled third-party components may carry additional notices. LGPL/GPL texts are included. The dynamically loaded Qt libraries remain separate from the application executable in `_internal`.

Qt source and license information: https://www.qt.io/download-open-source , https://code.qt.io/ , https://doc.qt.io/qtforpython-6/licenses.html . Obtain the corresponding Qt/PySide source for the version recorded in the bundle's metadata when preparing a redistribution. This package does not include Qt WebEngine or Qt PDF.

## Python, NumPy and PyInstaller

Python license information: https://docs.python.org/3/license.html . NumPy includes its BSD and bundled-library notices in its distribution metadata. PyInstaller's bootloader has an exception permitting distribution of applications built with it; its license/exception is included with the build metadata. Source projects: https://numpy.org/ and https://pyinstaller.org/ .

Additional dependent system libraries collected during a Linux build retain their original licenses. Where the build host provides Debian package copyright files, those notices and source package versions are collected in `licenses/debian/` for the discovered native dependencies. Graphics/audio drivers and the base operating system remain supplied by the destination system.
