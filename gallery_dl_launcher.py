"""PyInstaller sidecar launcher for gallery-dl."""
import runpy

if __name__ == "__main__":
    runpy.run_module("gallery_dl", run_name="__main__")
