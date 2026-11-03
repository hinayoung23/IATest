#import "NetworkTransport.h"
#import <CommonCrypto/CommonDigest.h>
#import <arpa/inet.h>
#import <errno.h>
#import <fcntl.h>
#import <sys/socket.h>
#import <unistd.h>
#import <string.h>

static void Need(BOOL valid) { if(!valid) @throw [NSException exceptionWithName:@"IATestObservation" reason:@"Pinned TLS observation unavailable or binding rejected" userInfo:nil]; }
static OSStatus ReadTLS(SSLConnectionRef connection, void *data, size_t *length) {
    size_t wanted=*length; ssize_t n=recv((int)(intptr_t)connection,data,wanted,0);
    *length=n>0?(size_t)n:0;
    if(n>0)return (size_t)n==wanted?noErr:errSSLWouldBlock;
    if(n==0)return errSSLClosedGraceful;
    return errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR?errSSLWouldBlock:errSSLClosedAbort;
}
static OSStatus WriteTLS(SSLConnectionRef connection, const void *data, size_t *length) {
    size_t wanted=*length; ssize_t n=send((int)(intptr_t)connection,data,wanted,0);
    *length=n>0?(size_t)n:0;
    if(n>0)return (size_t)n==wanted?noErr:errSSLWouldBlock;
    return errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR?errSSLWouldBlock:errSSLClosedAbort;
}
static BOOL Transfer(IATestSocket *socket, void *buffer, NSUInteger length, BOOL write) {
    NSUInteger offset=0; double deadline=NSProcessInfo.processInfo.systemUptime+60;
    while(offset<length&&NSProcessInfo.processInfo.systemUptime<deadline) {
        size_t done=0; OSStatus status=noErr;
        if(socket->tls)status=write?SSLWrite(socket->tls,(char *)buffer+offset,length-offset,&done):SSLRead(socket->tls,(char *)buffer+offset,length-offset,&done);
        else {ssize_t n=write?send(socket->fd,(char *)buffer+offset,length-offset,0):recv(socket->fd,(char *)buffer+offset,length-offset,0);if(n<=0)return NO;done=(size_t)n;}
        offset+=done;if(status!=noErr&&status!=errSSLWouldBlock)return NO;
    }
    return offset==length;
}
BOOL IATestSocketRead(IATestSocket *socket, void *buffer, NSUInteger length) { return Transfer(socket,buffer,length,NO); }
BOOL IATestSocketWrite(IATestSocket *socket, const void *buffer, NSUInteger length) { return Transfer(socket,(void *)buffer,length,YES); }
void IATestSocketClose(IATestSocket *socket) {
    if(socket->tls){CFRelease(socket->tls);socket->tls=NULL;}
    if(socket->fd>=0){shutdown(socket->fd,SHUT_RDWR);close(socket->fd);socket->fd=-1;}
}
IATestSocket IATestConnectObservation(NSDictionary *environment, NSString *role) {
    IATestSocket channel={.fd=-1,.tls=NULL};
    NSString *stage=@"configuration";
    @try {
        NSString *host=environment[@"IATEST_OBSERVATION_HOST"],*pin=environment[@"IATEST_OBSERVATION_PIN"];
        NSInteger port=[environment[@"IATEST_OBSERVATION_PORT"] integerValue];
        Need(host.length>0&&pin.length==64&&port>=1024&&port<=65535);
        struct sockaddr_in address={0};address.sin_family=AF_INET;address.sin_port=htons((uint16_t)port);Need(inet_pton(AF_INET,host.UTF8String,&address.sin_addr)==1);
        stage=@"tcp_connect";
        // iOS can reject the first local TCP request before the user answers
        // its permission alert. Retry only connection setup, before TLS/token
        // authentication and before any application commands can be sent.
        double connectDeadline=NSProcessInfo.processInfo.systemUptime+([role isEqual:@"control"]?30:3);
        while(YES) {
            channel.fd=socket(AF_INET,SOCK_STREAM,0);Need(channel.fd>=0);int yes=1;setsockopt(channel.fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
            int flags=fcntl(channel.fd,F_GETFL,0);Need(flags>=0&&fcntl(channel.fd,F_SETFL,flags|O_NONBLOCK)==0);
            int result=connect(channel.fd,(struct sockaddr *)&address,sizeof(address));int error=result==0?0:errno;
            if(error==EINPROGRESS) {
                double remaining=connectDeadline-NSProcessInfo.processInfo.systemUptime;Need(remaining>0);
                double wait=MIN(1.0,remaining);fd_set set;FD_ZERO(&set);FD_SET(channel.fd,&set);
                struct timeval limit={(int)wait,(int)((wait-(int)wait)*1000000)};
                int ready=select(channel.fd+1,NULL,&set,NULL,&limit);
                if(ready>0){socklen_t size=sizeof(error);Need(getsockopt(channel.fd,SOL_SOCKET,SO_ERROR,&error,&size)==0);}
                else error=ready==0?ETIMEDOUT:errno;
            }
            if(error==0){Need(fcntl(channel.fd,F_SETFL,flags)==0);break;}
            close(channel.fd);channel.fd=-1;errno=error;
            Need((error==EHOSTUNREACH||error==ENETUNREACH||error==ENETDOWN||error==ECONNREFUSED||error==ETIMEDOUT||error==EINTR)&&NSProcessInfo.processInfo.systemUptime<connectDeadline);
            usleep(200000);
        }
        struct timeval timeout={1,0};setsockopt(channel.fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));setsockopt(channel.fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
        stage=@"tls_configuration";
        channel.tls=SSLCreateContext(kCFAllocatorDefault,kSSLClientSide,kSSLStreamType);Need(channel.tls!=NULL);
        Need(SSLSetIOFuncs(channel.tls,ReadTLS,WriteTLS)==noErr&&SSLSetConnection(channel.tls,(SSLConnectionRef)(intptr_t)channel.fd)==noErr);
        Need(SSLSetProtocolVersionMin(channel.tls,kTLSProtocol12)==noErr&&SSLSetSessionOption(channel.tls,kSSLSessionOptionBreakOnServerAuth,true)==noErr);
        stage=@"tls_handshake";
        BOOL verified=NO;double deadline=NSProcessInfo.processInfo.systemUptime+10;
        while(YES){Need(NSProcessInfo.processInfo.systemUptime<deadline);OSStatus status=SSLHandshake(channel.tls);
            if(status==errSSLServerAuthCompleted){
                SecTrustRef trust=NULL;Need(SSLCopyPeerTrust(channel.tls,&trust)==noErr&&trust!=NULL);
                SecCertificateRef certificate=SecTrustGetCertificateAtIndex(trust,0);Need(certificate!=NULL);
                NSData *data=CFBridgingRelease(SecCertificateCopyData(certificate));unsigned char digest[CC_SHA256_DIGEST_LENGTH];CC_SHA256(data.bytes,(CC_LONG)data.length,digest);
                NSMutableString *actual=[NSMutableString string];for(NSUInteger i=0;i<sizeof(digest);i++)[actual appendFormat:@"%02x",digest[i]];
                verified=[actual isEqualToString:pin];CFRelease(trust);stage=@"certificate_pin";Need(verified);stage=@"tls_handshake";
            }else if(status==noErr){Need(verified);break;}else Need(status==errSSLWouldBlock);
        }
        stage=@"server_preamble";
        const char expected[]="IATEST_REVERSE_TLS/1\n";char preamble[sizeof(expected)-1];size_t count=sizeof(preamble);Need(IATestSocketRead(&channel,preamble,count)&&memcmp(preamble,expected,count)==0);
        NSDictionary *binding=@{@"transport":@"iatest-reverse-tls/1",@"session_id":environment[@"IATEST_SESSION"],@"token":environment[@"IATEST_TOKEN"],@"app_id":environment[@"IATEST_APP"],@"role":role};
        NSData *json=[NSJSONSerialization dataWithJSONObject:binding options:0 error:nil];Need(json.length>0&&json.length<=4096);uint32_t length=htonl((uint32_t)json.length);
        stage=@"session_binding";
        Need(IATestSocketWrite(&channel,&length,4)&&IATestSocketWrite(&channel,json.bytes,json.length));char ack[3];Need(IATestSocketRead(&channel,ack,3)&&memcmp(ack,"OK\n",3)==0);
        return channel;
    } @catch(NSException *error) {
        int socketError=errno;IATestSocketClose(&channel);
        @throw [NSException exceptionWithName:@"IATestObservation" reason:[NSString stringWithFormat:@"Pinned TLS observation failed at %@ (socket errno %d); check the device LAN connection and Local Network permission",stage,socketError] userInfo:nil];
    }
}
