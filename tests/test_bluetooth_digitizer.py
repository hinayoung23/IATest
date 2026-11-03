import re
import subprocess
from pathlib import Path

from iatest.mobile.hid_reports import touch_report


def test_native_digitizer_map_fits_gatt_and_validates_releases(tmp_path, java_bin):
    source = (
        Path(__file__).parents[1]
        / "iatest/mobile/native/android-hid/src/dev/iatest/mobile/hid"
    )
    harness = tmp_path / "DigitizerAcceptance.java"
    harness.write_text("""package dev.iatest.mobile.hid;
public class DigitizerAcceptance {
 static void check(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args){
  byte[] data=new byte[HidDigitizer.LENGTH];data[0]=2;data[1]=1;data[2]=0;data[3]=20;data[7]=1;data[8]=4;data[9]=30;
  check(HidDigitizer.valid(data)&&!HidDigitizer.released(data));
  byte[] up=HidDigitizer.release(data);check(HidDigitizer.released(up)&&up[2]==0&&up[8]==4&&up[9]==30);
  data[8]=0;check(!HidDigitizer.valid(data));data[8]=4;
  data[6]=(byte)128;check(!HidDigitizer.valid(data));data[6]=0;
  data[30]=1;check(!HidDigitizer.valid(data));data[30]=0;
  check(!HidDigitizer.valid(new byte[61]));data[0]=6;check(!HidDigitizer.valid(data));
  StringBuilder out=new StringBuilder();for(byte b:HidDigitizer.descriptor())out.append(String.format("%02x",b&255));
  System.out.println(out);
 }
}""")
    subprocess.run(
        [
            str(java_bin / "javac"),
            "-d",
            str(tmp_path),
            str(source / "HidDigitizer.java"),
            str(harness),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    output = subprocess.run(
        [
            str(java_bin / "java"),
            "-cp",
            str(tmp_path),
            "dev.iatest.mobile.hid.DigitizerAcceptance",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    descriptor = bytes.fromhex(output.stdout.strip())
    base = (
        (source / "MainActivity.java")
        .read_text(encoding="utf-8")
        .split("static byte[] reportDescriptor()", 1)[1]
        .split("void bleState", 1)[0]
    )
    base = bytes.fromhex("".join(re.findall(r'"([0-9a-f]+)"', base)))
    assert len(base + descriptor) <= 512
    offset = size = count = identifier = 0
    bits = {}
    while offset < len(descriptor):
        prefix = descriptor[offset]
        length = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(descriptor[offset + 1 : offset + 1 + length], "little")
        tag = prefix & 0xFC
        if tag == 0x84:
            identifier = value
        elif tag == 0x74:
            size = value
        elif tag == 0x94:
            count = value
        elif tag in (0x80, 0xB0):
            bits[tag, identifier] = bits.get((tag, identifier), 0) + size * count
        offset += 1 + length
    assert bits == {(0x80, 4): 31 * 8, (0xB0, 5): 8}
    assert len(touch_report({0: (20, 30), 4: (40, 50)}, {})[1:32]) == 31
