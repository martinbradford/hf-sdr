# Integration

## Background
SDR Play have two applications available to owners of RSP receivers : SDR Uno and SDR Connect.

SDR Uno is a mature product which has been around for many years. It has good functionality the user interface is significantly non-standard bit like Marmite you either like it or hate it. SDR play obtained the source from an independent developer a bit like me and have enhanced it over the years but they never really felt that they were in control of it. It implements diversity and some very powerful noise control functions the user interface is a very fragmented and not easy to operate. Also it is a strictly Windows application and SDR play felt the need to develop a new application which would work on other operating systems as well as Windows this application is SDR Connect. 

SDR Connect is their new application it is built using avalonia and supports Windows linux and macos at least. Development has been very slow and it is still lacking in functionality relative to SDR Uno but they are making an effort to plug the gaps. 

101Cats was developed before SDR Connect was released and its primary target is SDR Uno. Until recently there were too many gaps in the functionality of SDR Connect or 101Cats to target it. The most recent release of SDR Connect as plugged most of those gaps and 101Cats is now capable of operating with both SDR Uno and SDR connect.

## Integration mechanisms

### SDR Uno

SDR Uno supports two integration paths that are available to external applications. These are incoming CAT control via a virtual COM port and outgoing CAT control via Omnirig. Both are bi-directional, the distinction is which end of the channel can be considered the master. 

The incoming CAT control emulates a basic Kenwood radio command set. It supports basic functions like changing the VFO frequency and received mode. The full command set is documented somewhere on the SDR Play web site. 101Cats only uses the "set frequency" and "set mode" commands as described below. In SDR Uno configuration you can specify a COM port ID for this function - it will generally be a virtual COM port created by Com0Com or VSPE. This control path treats SDR Uno as the slave - SDR Uno will never initiate a transaction on this path, it simply listens for incoming CAT commands and responds to them.

The outgoing CAT control is implemented over Omnirig. This is free software created by Afreet Software - see https://www.dxatlas.com/OmniRig/. Omnirig is a CAT multiplexer that implements a generic interface to many different models of radio. It orchestrates interactions, ensuring that they are performed as atomic transactions with the radio and avoiding the risk of multiple applications clashing.

Omnirig is a mature application. It worke well, but uses legacy Windows IPC mechanisms - hence it is strictly Windows only. When SDR Play commenced the design of SDR Connect, OS portability was a key requirement and that stopped them using Omnirig in the new application.

The outgoing CAT control channel in SDR Uno was designed to connect to a physical radio and allow it to be controlled by SDR Uno functioning as the CAT control application. The SDR Uno main window includes an "RSYN" button which controls this. When RSYN is enabled, the RSP receiver under the control of SDR Uno is locked in sync with the physical radio - tuning either will result in the other following it. 

### SDR Connect

SDR Connect also implements incoming CAT control in much the same way as SDR Uno by emulating a basic Kenwood radio communicating over a virtual COM port. But, given the design objective of OS portability, the outgoing CAT control could not be implemented using Omnirig. SDR Play have, instead, implemented the Web Socket API which is far more powerful and includes a full command set to query and control the RSP receiver. The assumption is that developers will make use of this API to implement all the future extensions to the product. There is already a third-party application available to implement the full outgoing CAT control that is missing in the shipped product. I have experimented with bolting that API into 101Cats. I will probably complete that task in order to support other 101Cats users who don't wish to switch to our new SDR application, but I would now expect to use our application in preference to SDR Connect. 

## SDR Uno "quirks"

101Cats fully integrates with SDR Uno via the two exposed CAT pathways. Initially, the integration was only via the Omnirig outgoing CAT control channel. I create a virtual COM port and configure OmniRig to connect to it with the radio specified as "FTdx101D". The other side of that virtual COM port is connected into 101Cats like any other third-party application and SDR Uno is configured to use OmniRig for CAT control of an attached radio. This works fine and the FTdx101D is synchronized with the RSP Duo - tune either and the other follows. 

The problem comes if I turn off RSYN in SDR Uno, thus allowing me to use the RSP Duo as a third receiver while enabling the receiver in the FTdx101D. This is a special case of a more generic problem that we must address in the design of our SDR application - how to manage multiple virtual receivers created in the SDR application. As an example, imagine that I've been tuning round and found a station that I would like to talk to, but he has a long queue of other stations wanting to contact him. I unlock the synchronization, enable the receiver in the FTdx101D and listen to him while continuing to explore the band using the RSP Duo. In principle, I could find other stations that I want to monitor and create a virtual receiver for each of them. Again, this will work fine.

The problem comes when I want to restore the synchronization. I have the FTdx101D tuned to one frequency and one or more virtual receivers on the RSP Duo tuned to other frequencies, but SDR Uno has no formal concept of which is the master and which are the slaves. If I turn on RSYN, it is not clear which device will take control. There would appear to be separate polling loops running, monitoring the various devices and implementing the synchronization - after I turn on RSYN, the first of those polling loops to run will take control and the other devices will be tuned to its frequency. I may be lucky, but there is a non-zero risk that I will lose the frequency that I actually wanted to communicate on.

101Cats solves this by using the incoming CAT control channel. The two buttons, "To SDR" and "From SDR" in the right hand button bar either send the frequency from 101Cats to SDR Uno, or retrieve the frequency and mode from SDR Uno. In this way, I can force the synchronization to occur in the direction I want before I turn it on on the SDR Uno main window. It is a bit tedious and can be error prone, so we need to give it some further thought in the implementation of the new application.

