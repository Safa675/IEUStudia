"""Entry point of the Windows release build (PyInstaller). One exe runs everything:

    IEUStudia.exe                 the app
    IEUStudia.exe --agent         the tray helper (no window)
    IEUStudia.exe --splash …      the start-up window (started by the app itself)
    IEUStudia.exe --selftest out.json   checks the build (imports, bundled files) and exits
"""

from studia.app import main

if __name__ == "__main__":
    main()
