#import <Foundation/Foundation.h>
#import <Security/SecureTransport.h>
typedef struct { int fd; SSLContextRef tls; } IATestSocket;
IATestSocket IATestConnectObservation(NSDictionary *environment, NSString *role);
BOOL IATestSocketRead(IATestSocket *socket, void *buffer, NSUInteger length);
BOOL IATestSocketWrite(IATestSocket *socket, const void *buffer, NSUInteger length);
void IATestSocketClose(IATestSocket *socket);
