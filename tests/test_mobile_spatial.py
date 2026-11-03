import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from iatest.mobile.runtime import MobileSession


def session(platform, enabled=True):
    value = MobileSession.__new__(MobileSession)
    value.profile = {"platform": platform}
    value.device = SimpleNamespace(
        semantic=SimpleNamespace(
            capabilities={"spatial_bounds": enabled},
            request=lambda op, args, **kwargs: args,
        )
    )
    return value


def test_spatial_points_on_ios_and_integer_pixels_on_android():
    rectangle = [10.25, 20.5, 99.75, 40.5]
    assert session("ios").query({"bounds": rectangle})["target"]["bounds"] == rectangle
    with pytest.raises(ValueError, match="integer pixels"):
        session("android").query({"bounds": rectangle})
    with pytest.raises(ValueError, match="Rebuild"):
        session("ios", False).query({"bounds": rectangle})


@pytest.mark.parametrize(
    "rectangle", [[1, 2, 1, 4], [1, 2, 3, 1], [0, 0, float("nan"), 2]]
)
def test_spatial_invalid_geometry_is_rejected_before_device_request(rectangle):
    with pytest.raises(ValueError):
        session("ios").query({"bounds": rectangle})


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("clang"), reason="macOS native compile"
)
def test_native_spatial_exact_fractional_rectangle_and_invalid_values(tmp_path):
    native = Path(__file__).resolve().parents[1] / "iatest/mobile/native/ios"
    source, executable = tmp_path / "bounds.m", tmp_path / "bounds-check"
    source.write_text("""#import "SpatialBounds.h"
int main(void) {@autoreleasepool {
    NSArray *r=@[@10.25,@20.5,@99.75,@40.5];
    if(!IATestBoundsMatch(CGRectMake(10.25,20.5,89.5,20),r))return 1;
    if(IATestBoundsMatch(CGRectMake(10.25,20.5,89.4,20),r))return 2;
    if(IATestBoundsValid(@[@0,@0,@0,@1]))return 3;
    if(IATestBoundsValid(@[@NO,@0,@1,@1]))return 4;
    if(IATestBoundsValid(@[@0,@0,@(NAN),@1]))return 5;
    if(IATestBoundsValid(@[@0,@0,@32769,@1]))return 6;
    if(IATestBoundsMatch(CGRectNull,r))return 7;
    if(IATestBoundsValid(@[@0,@0,@1]))return 8;
    return 0;
}}
""")
    subprocess.run(
        [
            "clang",
            "-fobjc-arc",
            "-framework",
            "Foundation",
            "-framework",
            "CoreGraphics",
            "-I",
            str(native),
            str(source),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
