#if DEBUG
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
FOUNDATION_EXPORT BOOL IATestBridgePublish(NSDictionary<NSString *,NSNumber *> *ready,
                                         NSDictionary<NSString *,NSString *> *identifiers,
                                         NSError **error);
FOUNDATION_EXPORT BOOL IATestBridgePublishInputState(NSDictionary<NSString *,NSNumber *> *ready,
    NSDictionary<NSString *,NSString *> *identifiers, NSDictionary<NSString *,UIView<UITextInput> *> *inputs, NSError **error);
// Pass the App's existing MTLDevice; the SDK never creates a GPU device.
FOUNDATION_EXPORT void IATestBridgeObserveMetalDevice(id device);
// Register each selected buffer before commit. Measures registered buffers only.
FOUNDATION_EXPORT void IATestBridgeObserveMetalCommandBuffer(id commandBuffer);
#endif
