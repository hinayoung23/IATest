import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("clang"),
    reason="Requires macOS Foundation and clang",
)
def test_native_ios_text_distinguishes_static_labels_from_empty_editor_values(tmp_path):
    native = Path(__file__).resolve().parents[1] / "iatest/mobile/native/ios"
    source, executable = tmp_path / "text.m", tmp_path / "text-check"
    source.write_text("""#import "ElementText.h"
#import "VisualFreshness.h"
int main(void) {@autoreleasepool {
    if(![IATestElementText(@"Ready",@"",NO) isEqual:@"Ready"])return 1;
    if(![IATestElementText(@"Save",nil,NO) isEqual:@"Save"])return 2;
    if(![IATestElementText(@"Nickname",@"",YES) isEqual:@""])return 3;
    if(![IATestElementText(@"Nickname",@"iatest_random",YES) isEqual:@"iatest_random"])return 4;
    if(![IATestElementText(@"Saved: iatest_random",@"",NO) isEqual:@"Saved: iatest_random"])return 5;
    if(!IATestVisualFresh(1000,1000,2500,2000))return 6;
    if(IATestVisualFresh(1000,1000,3001,2000))return 7;
    if(IATestVisualFresh(1000,2000,2500,2000))return 8;
    if(IATestVisualFresh(3000,3000,2500,2000))return 9;
    if(IATestVisualFresh(1000,1000,2500,5000))return 10;
    if(!IATestEditorValueAmbiguous(@"Nickname",@"Nickname"))return 11;
    if(IATestEditorValueAmbiguous(@"iatest_random",@"Nickname"))return 12;
    if(IATestEditorValueAmbiguous(@"",@""))return 13;
    if(IATestEditorValueAmbiguous(nil,@"Nickname"))return 14;
    return 0;
}}
""")
    subprocess.run(
        [
            "clang",
            "-fobjc-arc",
            "-framework",
            "Foundation",
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
