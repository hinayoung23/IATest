#import <Foundation/Foundation.h>

// XCTest exposes UILabel text through label, and editor text through value.
// XCTest can expose a placeholder as the value of an empty editor. Preserve
// the observation but mark that ambiguity; equality cannot prove user input.
static inline NSString *IATestElementText(NSString *label,id value,BOOL editable) {
    return editable?([value isKindOfClass:NSString.class]?value:@""):(label?:@"");
}
static inline BOOL IATestEditorValueAmbiguous(id value,NSString *placeholder) {
    return placeholder.length>0&&[value isKindOfClass:NSString.class]&&[value isEqual:placeholder];
}
