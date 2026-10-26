#!/usr/bin/env python3
"""Build the local Windows VSIX for an absolute Linux marker path (no downloads)."""

import argparse
import json
from pathlib import Path
import re
from xml.sax.saxutils import escape, quoteattr
import zipfile


ROOT = Path(__file__).resolve().parent
EXTENSION = ROOT / "extension"
FILES = ("package.json", "extension.js", "piano.wav", "LICENSE.txt", "README.txt")


def package(marker_path, output_dir=None, extension_dir=None):
    if not isinstance(marker_path, str) or not marker_path.startswith("/"):
        raise ValueError("--marker-path must be an absolute Linux path")
    extension = Path(extension_dir) if extension_dir is not None else EXTENSION
    metadata = json.loads((extension / "package.json").read_text(encoding="utf-8"))
    script = (extension / "extension.js").read_text(encoding="utf-8")
    script, count = re.subn(
        r"^const EVENT_FILE = .*;$|^const EVENT_FILE = .*; // Set by .*$",
        lambda match: "const EVENT_FILE = " + json.dumps(marker_path) + ";",
        script, flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Expected exactly one EVENT_FILE constant in extension.js")
    files = {name: (extension / name).read_bytes() for name in FILES}
    files["extension.js"] = script.encode("utf-8")
    manifest = '''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={name} Version={version} Publisher={publisher} />
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Categories>Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={engine} />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="ui" />
      <Property Id="Microsoft.VisualStudio.Code.ExecutesCode" Value="true" />
    </Properties>
    <License>extension/LICENSE.txt</License>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code" /></Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
  </Assets>
</PackageManifest>
'''.format(
        name=quoteattr(metadata["name"]), version=quoteattr(metadata["version"]),
        publisher=quoteattr(metadata["publisher"]), display=escape(metadata["displayName"]),
        description=escape(metadata["description"]), engine=quoteattr(metadata["engines"]["vscode"]),
    )
    content_types = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="wav" ContentType="audio/wav" />
  <Default Extension="txt" ContentType="text/plain" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
</Types>
'''
    entries = {"extension.vsixmanifest": manifest.encode("utf-8"), "[Content_Types].xml": content_types.encode("utf-8")}
    entries.update({"extension/" + name: content for name, content in files.items()})
    destination = Path(output_dir) if output_dir is not None else ROOT.parent / "dist"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "{}-{}.vsix".format(metadata["name"], metadata["version"])
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or archive.read("extension/extension.js") != files["extension.js"]:
            raise ValueError("Generated VSIX failed its integrity check")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker-path", required=True, help="absolute Linux completion-piano/last-completion.json path")
    parser.add_argument("--output-dir", type=Path, help="artifact directory (default: repository dist/)")
    args = parser.parse_args()
    try:
        output = package(args.marker_path, args.output_dir)
    except (OSError, ValueError) as error:
        parser.exit(1, "Build failed: {}\n".format(error))
    print(output)


if __name__ == "__main__":
    main()
