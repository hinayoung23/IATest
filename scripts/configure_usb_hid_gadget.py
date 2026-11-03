"""Configure a new keyboard/mouse gadget on an explicitly selected Linux UDC.

Run on the external bridge, not the Mac or phone. Existing gadgets are untouched.
VID/PID must be supplied by the hardware operator. No networking or secrets.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from iatest.mobile.hid_reports import ABSOLUTE_MOUSE, touch_descriptor

# Boot keyboard (8 bytes) and three-button relative mouse + wheel (4 bytes).
KEYBOARD = bytes.fromhex(
    "05010906a101050719e029e715002501750195088102950175088103"
    "95057501050819012905910295017503910395067508150026a400"
    "050719002aa4008100c0"
)
MOUSE = bytes.fromhex(
    "05010902a1010901a10005091901290315002501950375018102"
    "95017505810305010930093109381581257f750895038106c0c0"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udc", required=True)
    parser.add_argument(
        "--vendor-id", required=True, help="Operator-authorized USB VID, hexadecimal"
    )
    parser.add_argument(
        "--product-id", required=True, help="Operator-authorized USB PID, hexadecimal"
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--absolute-mouse", action="store_true")
    parser.add_argument("--digitizer", action="store_true", help="Ten-contact digitizer; bridge kernel must implement HID GET_REPORT ioctl")
    parser.add_argument(
        "--apply", action="store_true", help="Actually create and bind a new gadget"
    )
    args = parser.parse_args()
    if any(
        not re.fullmatch(r"(?:0x)?[a-fA-F0-9]{4}", value)
        for value in (args.vendor_id, args.product_id)
    ):
        raise ValueError("USB VID/PID must contain four hexadecimal digits")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
        for value in (args.udc, args.serial)
    ):
        raise ValueError("Invalid UDC/serial")
    if not args.apply:
        print(
            "Dry run: creates /sys/kernel/config/usb_gadget/iatest_hid with an 8-byte keyboard and 4-byte mouse"
            + (", 6-byte absolute pointer" if args.absolute_mouse else "")
            + (", 62-byte ten-contact digitizer" if args.digitizer else "")
            + "; --apply enables it."
        )
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError(
            "USB gadget configuration requires root on the external Linux bridge"
        )
    base = Path("/sys/kernel/config/usb_gadget")
    if not base.is_dir() or not (Path("/sys/class/udc") / args.udc).is_dir():
        raise ValueError("Load libcomposite/configfs and select an existing UDC")
    if any(p.read_text().strip() for p in base.glob("*/UDC")):
        raise ValueError("An existing gadget is bound; do not replace it automatically")
    root = base / "iatest_hid"
    root.mkdir()  # Refuse to overwrite an existing or partially configured gadget.
    try:
        (root / "idVendor").write_text("0x" + args.vendor_id.removeprefix("0x"))
        (root / "idProduct").write_text("0x" + args.product_id.removeprefix("0x"))
        strings = root / "strings/0x409"
        strings.mkdir()
        for key, value in {
            "serialnumber": args.serial,
            "manufacturer": "IATest operator bridge",
            "product": "IATest USB keyboard and mouse",
        }.items():
            (strings / key).write_text(value)
        config = root / "configs/c.1"
        config.mkdir()
        (config / "MaxPower").write_text("100")
        functions = [
            ("keyboard", 1, 8, KEYBOARD),
            ("mouse", 0, 4, MOUSE),
        ]
        if args.absolute_mouse:
            functions.append(("absolute_mouse", 0, 6, ABSOLUTE_MOUSE))
        if args.digitizer:
            functions.append(("digitizer", 0, 62, touch_descriptor()))
        for name, protocol, length, descriptor in functions:
            function = root / ("functions/hid." + name)
            function.mkdir()
            for key, value in {
                "protocol": protocol,
                "subclass": 1 if name == "keyboard" else 0,
                "report_length": length,
            }.items():
                (function / key).write_text(str(value))
            (function / "report_desc").write_bytes(descriptor)
            (config / ("hid." + name)).symlink_to(function)
        (root / "UDC").write_text(args.udc)
    except BaseException as exc:
        # Never delete partial configfs state or touch another gadget on failure.
        raise RuntimeError(
            "Owned gadget setup is incomplete at "
            + str(root)
            + "; inspect it before retrying"
        ) from exc
    print(
        "Configured "
        + str(root)
        + ". Verify the actual /dev/hidgX assignments before starting the bridge."
    )


if __name__ == "__main__":
    main()
